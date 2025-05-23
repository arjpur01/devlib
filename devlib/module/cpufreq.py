#    Copyright 2014-2025 ARM Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from devlib.module import Module
from devlib.exception import TargetStableError
from devlib.utils.misc import memoized
import devlib.utils.asyn as asyn
from typing import (TYPE_CHECKING, Dict, List, Tuple, Union,
                    cast, Optional, Any, Set, Coroutine)
from collections.abc import AsyncGenerator
if TYPE_CHECKING:
    from devlib.target import Target

# a dict of governor name and a list of it tunables that can't be read
WRITE_ONLY_TUNABLES: Dict[str, List[str]] = {
    'interactive': ['boostpulse']
}


class CpufreqModule(Module):
    """
    ``cpufreq`` is the kernel subsystem for managing DVFS (Dynamic Voltage and
    Frequency Scaling). It allows controlling frequency ranges and switching
    policies (governors). The ``devlib`` module exposes the following interface

    .. note:: On ARM big.LITTLE systems, all cores on a cluster (usually all cores
            of the same type) are in the same frequency domain, so setting
            ``cpufreq`` state on one core on a cluster will affect all cores on
            that cluster. Because of this, some devices only expose cpufreq sysfs
            interface (which is what is used by the ``devlib`` module) on the
            first cpu in a cluster. So to keep your scripts portable, always use
            the fist (online) CPU in a cluster to set ``cpufreq`` state.
    """
    name: str = 'cpufreq'

    @staticmethod
    @asyn.asyncf
    async def probe(target: 'Target') -> bool:
        paths_tmp: List[Tuple[bool, str]] = [
            # x86 with Intel P-State driver
            (target.abi == 'x86_64', '/sys/devices/system/cpu/intel_pstate'),
            # Generic CPUFreq support (single policy)
            (True, '/sys/devices/system/cpu/cpufreq/policy0'),
            # Generic CPUFreq support (per CPU policy)
            (True, '/sys/devices/system/cpu/cpu0/cpufreq'),
        ]
        paths: List[str] = [
            path[1] for path in paths_tmp
            if path[0]
        ]

        exists = await target.async_manager.map_concurrently(
            target.file_exists.asyn,
            paths,
        )

        return any(exists.values())

    def __init__(self, target: 'Target'):
        super(CpufreqModule, self).__init__(target)
        self._governor_tunables: Dict[str, Tuple[str, bool, List[str]]] = {}

    @asyn.asyncf
    @asyn.memoized_method
    async def list_governors(self, cpu: Union[int, str]) -> List[str]:
        """List cpufreq governors available for the specified cpu. Returns a list of
        strings.

        :param cpu: The cpu; could be a numeric or the corresponding string (e.g.
                    ``1`` or ``"cpu1"``).
        """
        if isinstance(cpu, int):
            cpu = 'cpu{}'.format(cpu)
        sysfile: str = '/sys/devices/system/cpu/{}/cpufreq/scaling_available_governors'.format(cpu)
        output: str = await self.target.read_value.asyn(sysfile)
        return output.strip().split()

    @asyn.asyncf
    async def get_governor(self, cpu: Union[int, str]) -> str:
        """
        Returns the name of the currently set governor for the specified cpu.

        :param cpu: The cpu; could be a numeric or the corresponding string (e.g.
                    ``1`` or ``"cpu1"``).
        """
        if isinstance(cpu, int):
            cpu = 'cpu{}'.format(cpu)
        sysfile: str = '/sys/devices/system/cpu/{}/cpufreq/scaling_governor'.format(cpu)
        return await self.target.read_value.asyn(sysfile)

    @asyn.asyncf
    async def set_governor(self, cpu: Union[int, str], governor: str, **kwargs) -> None:
        """
        Set the governor for the specified CPU.
        See https://www.kernel.org/doc/Documentation/cpu-freq/governors.txt

        :param cpu: The CPU for which the governor is to be set. It could be a numeric or the corresponding string (e.g.
        ``1`` or ``"cpu1"``).
        :param governor: The name of the governor to be used. This must be
                         supported by the specific device (as returned by ``list_governors()``.

        Additional keyword arguments can be used to specify governor tunables for
        governors that support them.

        :note: On big.LITTLE all cores in a cluster must be using the same governor.
               Setting the governor on any core in a cluster will also set it on all
               other cores in that cluster.

        :raises: TargetStableError if governor is not supported by the CPU, or if,
                 for some reason, the governor could not be set.

        """
        if isinstance(cpu, int):
            cpu = 'cpu{}'.format(cpu)
        supported: List[str] = await self.list_governors.asyn(cpu)
        if governor not in supported:
            raise TargetStableError('Governor {} not supported for cpu {}'.format(governor, cpu))
        sysfile = '/sys/devices/system/cpu/{}/cpufreq/scaling_governor'.format(cpu)
        await self.target.write_value.asyn(sysfile, governor)
        await self.set_governor_tunables.asyn(cpu, governor, **kwargs)

    @asyn.asynccontextmanager
    async def use_governor(self, governor: str, cpus: Optional[List[str]] = None, **kwargs) -> AsyncGenerator:
        """
        Use a given governor, then restore previous governor(s)

        :param governor: Governor to use on all targeted CPUs (see :meth:`set_governor`)

        :param cpus: CPUs affected by the governor change (all by default)

        :Keyword Arguments: Governor tunables, See :meth:`set_governor_tunables`
        """
        if not cpus:
            cpus = await self.target.list_online_cpus.asyn()

        async def get_cpu_info(cpu) -> List[Any]:
            return await self.target.async_manager.concurrently((
                self.get_affected_cpus.asyn(cpu),
                self.get_governor.asyn(cpu),
                self.get_governor_tunables.asyn(cpu),
                # We won't always use the frequency, but it's much quicker to
                # do concurrently anyway so do it now
                self.get_frequency.asyn(cpu),
            ))

        cpus_infos: Dict[int, List[Any]] = await self.target.async_manager.map_concurrently(get_cpu_info, cpus)

        # Setting a governor & tunables for a cpu will set them for all cpus in
        # the same cpufreq policy, so only manipulating one cpu per domain is
        # enough
        domains: Set[Any] = set(
            info[0][0]
            for info in cpus_infos.values()
        )

        await self.target.async_manager.concurrently(
            self.set_governor.asyn(cpu, governor, **kwargs)
            for cpu in domains
        )

        try:
            yield
        finally:
            async def set_per_cpu_tunables(cpu: int) -> None:
                domain, prev_gov, tunables, freq = cpus_infos[cpu]
                # Per-cpu tunables are safe to set concurrently
                await self.set_governor_tunables.asyn(cpu, prev_gov, per_cpu=True, **tunables)
                # Special case for userspace, frequency is not seen as a tunable
                if prev_gov == "userspace":
                    await self.set_frequency.asyn(cpu, freq)

            per_cpu_tunables: Coroutine = self.target.async_manager.concurrently(
                set_per_cpu_tunables(cpu)
                for cpu in domains
            )
            per_cpu_tunables.__qualname__ = 'CpufreqModule.use_governor.<locals>.per_cpu_tunables'

            # Non-per-cpu tunables have to be set one after the other, for each
            # governor that we had to deal with.
            global_tunables_dict: Dict[str, Tuple[int, Dict[str, List[str]]]] = {
                prev_gov: (cpu, tunables)
                for cpu, (domain, prev_gov, tunables, freq) in cpus_infos.items()
            }

            global_tunables: Coroutine = self.target.async_manager.concurrently(
                self.set_governor_tunables.asyn(cpu, gov, per_cpu=False, **tunables)
                for gov, (cpu, tunables) in global_tunables_dict.items()
            )
            global_tunables.__qualname__ = 'CpufreqModule.use_governor.<locals>.global_tunables'

            # Set the governor first
            await self.target.async_manager.concurrently(
                self.set_governor.asyn(cpu, cpus_infos[cpu][1])
                for cpu in domains
            )
            # And then set all the tunables concurrently. Each task has a
            # specific and non-overlapping set of file to write.
            await self.target.async_manager.concurrently(
                (per_cpu_tunables, global_tunables)
            )

    @asyn.asyncf
    async def _list_governor_tunables(self, cpu: Union[int, str],
                                      governor: Optional[str] = None) -> Tuple[str, bool, List[str]]:
        """
        helper function for list_governor_tunables
        """
        if isinstance(cpu, int):
            cpu = 'cpu{}'.format(cpu)

        if governor is None:
            governor = await self.get_governor.asyn(cpu)

        try:
            if not governor:
                raise TargetStableError
            return self._governor_tunables[governor]
        except KeyError:
            for per_cpu, path in (
                (True, '/sys/devices/system/cpu/{}/cpufreq/{}'.format(cpu, governor)),
                # On old kernels
                (False, '/sys/devices/system/cpu/cpufreq/{}'.format(governor)),
            ):
                try:
                    tunables: List[str] = await self.target.list_directory.asyn(path)
                except TargetStableError:
                    continue
                else:
                    break
            else:
                per_cpu = False
                tunables = []

            data: Tuple[str, bool, List[str]] = (cast(str, governor), per_cpu, tunables)
            if governor:
                self._governor_tunables[governor] = data
            return data

    @asyn.asyncf
    async def list_governor_tunables(self, cpu: Union[int, str]) -> Tuple[str, bool, List[str]]:
        """
        List the tunables for the specified cpu's current governor.

        :param cpu: The cpu; could be a numeric or the corresponding string (e.g.
            ``1`` or ``"cpu1"``).
        """
        _, _, tunables = await self._list_governor_tunables.asyn(cpu)
        return tunables

    @asyn.asyncf
    async def get_governor_tunables(self, cpu: Union[int, str]) -> Dict[str, List[str]]:
        """
        Return a dict with the values of the specified CPU's current governor.

        :param cpu: The cpu; could be a numeric or the corresponding string (e.g.
            ``1`` or ``"cpu1"``).
        """
        if isinstance(cpu, int):
            cpu = 'cpu{}'.format(cpu)
        governor: str
        tunable_list: List[str]
        governor, _, tunable_list = await self._list_governor_tunables.asyn(cpu)

        write_only: Set[str] = set(WRITE_ONLY_TUNABLES.get(governor, []))
        tunable_list = [
            tunable
            for tunable in tunable_list
            if tunable not in write_only
        ]

        tunables: Dict[str, List[str]] = {}

        async def get_tunable(tunable: str) -> str:
            try:
                path: str = '/sys/devices/system/cpu/{}/cpufreq/{}/{}'.format(cpu, governor, tunable)
                x: str = await self.target.read_value.asyn(path)
            except TargetStableError:  # May be an older kernel
                path = '/sys/devices/system/cpu/cpufreq/{}/{}'.format(governor, tunable)
                x = await self.target.read_value.asyn(path)
            return x

        tunables = await self.target.async_manager.map_concurrently(get_tunable, tunable_list)
        return tunables

    @asyn.asyncf
    async def set_governor_tunables(self, cpu: Union[int, str], governor: Optional[str] = None,
                                    per_cpu: Optional[bool] = None, **kwargs) -> None:
        """
        Set tunables for the specified governor. Tunables should be specified as
        keyword arguments. Which tunables and values are valid depends on the
        governor.

        :param cpu: The cpu for which the governor will be set. ``int`` or
                    full cpu name as it appears in sysfs, e.g. ``cpu0``.
        :param governor: The name of the governor. Must be all lower case.
        :param per_cpu: If ``None``, both per-cpu and global governor tunables
            will be set. If ``True``, only per-CPU tunables will be set and if
            ``False``, only global tunables will be set.

        The rest should be keyword parameters mapping tunable name onto the value to
        be set for it.

        :raises: TargetStableError if governor specified is not a valid governor name, or if
                 a tunable specified is not valid for the governor, or if could not set
                 tunable.

        """
        if not kwargs:
            return
        if isinstance(cpu, int):
            cpu = 'cpu{}'.format(cpu)

        gov_per_cpu: bool
        valid_tunables: List[str]
        governor, gov_per_cpu, valid_tunables = await self._list_governor_tunables.asyn(cpu, governor=governor)
        for tunable, value in kwargs.items():
            if tunable in valid_tunables:
                if per_cpu is not None and gov_per_cpu != per_cpu:
                    continue

                if gov_per_cpu:
                    path: str = '/sys/devices/system/cpu/{}/cpufreq/{}/{}'.format(cpu, governor, tunable)
                else:
                    path = '/sys/devices/system/cpu/cpufreq/{}/{}'.format(governor, tunable)

                await self.target.write_value.asyn(path, value)
            else:
                message: str = 'Unexpected tunable {} for governor {} on {}.\n'.format(tunable, governor, cpu)
                message += 'Available tunables are: {}'.format(valid_tunables)
                raise TargetStableError(message)

    @asyn.asyncf
    @asyn.memoized_method
    async def list_frequencies(self, cpu: Union[int, str]) -> List[int]:
        """
        Returns a sorted list of frequencies supported by the cpu or an empty list
        if not could be found.
        :param cpu: The cpu; could be a numeric or the corresponding string (e.g.
       ``1`` or ``"cpu1"``).
        """
        if isinstance(cpu, int):
            cpu = 'cpu{}'.format(cpu)
        try:
            cmd: str = 'cat /sys/devices/system/cpu/{}/cpufreq/scaling_available_frequencies'.format(cpu)
            output: str = await self.target.execute.asyn(cmd)
            available_frequencies: List[int] = list(map(int, output.strip().split()))  # pylint: disable=E1103
        except TargetStableError:
            # On some devices scaling_frequencies  is not generated.
            # http://adrynalyne-teachtofish.blogspot.co.uk/2011/11/how-to-enable-scalingavailablefrequenci.html
            # Fall back to parsing stats/time_in_state
            path: str = '/sys/devices/system/cpu/{}/cpufreq/stats/time_in_state'.format(cpu)
            try:
                out_iter: List[str] = cast(str, (await self.target.read_value.asyn(path))).split()
            except TargetStableError:
                if not self.target.file_exists(path):
                    # Probably intel_pstate. Can't get available freqs.
                    return []
                raise

            available_frequencies = list(map(int, reversed([f for f, _ in zip(out_iter, out_iter)])))
        return sorted(available_frequencies)

    @memoized
    def get_max_available_frequency(self, cpu: Union[str, int]) -> Optional[int]:
        """
        Returns the maximum available frequency for a given core or None if
        could not be found.
        """
        freqs = self.list_frequencies(cpu)
        return max(freqs) if freqs else None

    @memoized
    def get_min_available_frequency(self, cpu: Union[str, int]) -> Optional[int]:
        """
        Returns the minimum available frequency for a given core or None if
        could not be found.
        """
        freqs: List[int] = self.list_frequencies(cpu)
        return min(freqs) if freqs else None

    @asyn.asyncf
    async def get_min_frequency(self, cpu: Union[str, int]) -> int:
        """
        Returns the min frequency currently set for the specified CPU.

        Warning, this method does not check if the cpu is online or not. It will
        try to read the minimum frequency and the following exception will be
        raised ::

        :raises: TargetStableError if for some reason the frequency could not be read.

        """
        if isinstance(cpu, int):
            cpu = 'cpu{}'.format(cpu)
        sysfile: str = '/sys/devices/system/cpu/{}/cpufreq/scaling_min_freq'.format(cpu)
        return await self.target.read_int.asyn(sysfile)

    @asyn.asyncf
    async def set_min_frequency(self, cpu: Union[str, int], frequency: Union[int, str], exact: bool = True) -> None:
        """
        Set's the minimum value for CPU frequency. Actual frequency will
        depend on the Governor used and may vary during execution. The value should be
        either an int or a string representing an integer. The Value must also be
        supported by the device. The available frequencies can be obtained by calling
        get_frequencies() or examining

        /sys/devices/system/cpu/cpuX/cpufreq/scaling_frequencies

        on the device.

        :raises: TargetStableError if the frequency is not supported by the CPU, or if, for
                 some reason, frequency could not be set.
        :raises: ValueError if ``frequency`` is not an integer.

        """
        if isinstance(cpu, int):
            cpu = 'cpu{}'.format(cpu)
        available_frequencies: List[int] = await self.list_frequencies.asyn(cpu)
        try:
            value = int(frequency)
            if exact and available_frequencies and value not in available_frequencies:
                raise TargetStableError('Can\'t set {} frequency to {}\nmust be in {}'.format(cpu,
                                                                                              value,
                                                                                              available_frequencies))
            sysfile: str = '/sys/devices/system/cpu/{}/cpufreq/scaling_min_freq'.format(cpu)
            await self.target.write_value.asyn(sysfile, value)
        except ValueError:
            raise ValueError('Frequency must be an integer; got: "{}"'.format(frequency))

    @asyn.asyncf
    async def get_frequency(self, cpu: Union[str, int], cpuinfo: bool = False) -> int:
        """
        Returns the current frequency currently set for the specified CPU.

        Warning, this method does not check if the cpu is online or not. It will
        try to read the current frequency and the following exception will be
        raised ::

        :param cpuinfo: Read the value in the cpuinfo interface that reflects
                        the actual running frequency.

        :raises: TargetStableError if for some reason the frequency could not be read.

        """
        if isinstance(cpu, int):
            cpu = 'cpu{}'.format(cpu)

        sysfile: str = '/sys/devices/system/cpu/{}/cpufreq/{}'.format(
            cpu,
            'cpuinfo_cur_freq' if cpuinfo else 'scaling_cur_freq')
        return await self.target.read_int.asyn(sysfile)

    @asyn.asyncf
    async def set_frequency(self, cpu: Union[str, int], frequency: Union[int, str], exact: bool = True) -> None:
        """
        Sets the minimum value for CPU frequency. Actual frequency will
        depend on the Governor used and may vary during execution. The value should be
        either an int or a string representing an integer.

        `set_frequency`` is only available if the current governor is ``userspace``.

        If ``exact`` flag is set (the default), the Value must also be supported by
        the device. The available frequencies can be obtained by calling
        get_frequencies() or examining

        /sys/devices/system/cpu/cpuX/cpufreq/scaling_frequencies

        on the device (if it exists).

        :raises: TargetStableError if the frequency is not supported by the CPU, or if, for
                 some reason, frequency could not be set.
        :raises: ValueError if ``frequency`` is not an integer.

        """
        if isinstance(cpu, int):
            cpu = 'cpu{}'.format(cpu)
        try:
            value = int(frequency)
            if exact:
                available_frequencies: List[int] = await self.list_frequencies.asyn(cpu)
                if available_frequencies and value not in available_frequencies:
                    raise TargetStableError('Can\'t set {} frequency to {}\nmust be in {}'.format(cpu,
                                                                                                  value,
                                                                                                  available_frequencies))
            if await self.get_governor.asyn(cpu) != 'userspace':
                raise TargetStableError('Can\'t set {} frequency; governor must be "userspace"'.format(cpu))
            sysfile: str = '/sys/devices/system/cpu/{}/cpufreq/scaling_setspeed'.format(cpu)
            await self.target.write_value.asyn(sysfile, value, verify=False)
            cpuinfo: int = await self.get_frequency.asyn(cpu, cpuinfo=True)
            if cpuinfo != value:
                self.logger.warning(
                    'The cpufreq value has not been applied properly cpuinfo={} request={}'.format(cpuinfo, value))
        except ValueError:
            raise ValueError('Frequency must be an integer; got: "{}"'.format(frequency))

    @asyn.asyncf
    async def get_max_frequency(self, cpu: Union[str, int]) -> int:
        """
        Returns the max frequency currently set for the specified CPU.

        Warning, this method does not check if the cpu is online or not. It will
        try to read the maximum frequency and the following exception will be
        raised ::

        :raises: TargetStableError if for some reason the frequency could not be read.
        """
        if isinstance(cpu, int):
            cpu = 'cpu{}'.format(cpu)
        sysfile: str = '/sys/devices/system/cpu/{}/cpufreq/scaling_max_freq'.format(cpu)
        return await self.target.read_int.asyn(sysfile)

    @asyn.asyncf
    async def set_max_frequency(self, cpu: Union[str, int],
                                frequency: Union[str, int], exact: bool = True) -> None:
        """
        Set's the maximum value for CPU frequency. Actual frequency will
        depend on the Governor used and may vary during execution. The value should be
        either an int or a string representing an integer. The Value must also be
        supported by the device. The available frequencies can be obtained by calling
        get_frequencies() or examining

        /sys/devices/system/cpu/cpuX/cpufreq/scaling_frequencies

        on the device.

        :raises: TargetStableError if the frequency is not supported by the CPU, or if, for
                 some reason, frequency could not be set.
        :raises: ValueError if ``frequency`` is not an integer.

        """
        if isinstance(cpu, int):
            cpu = 'cpu{}'.format(cpu)
        available_frequencies = await self.list_frequencies.asyn(cpu)
        try:
            value = int(frequency)
            if exact and available_frequencies and value not in available_frequencies:
                raise TargetStableError('Can\'t set {} frequency to {}\nmust be in {}'.format(cpu,
                                                                                              value,
                                                                                              available_frequencies))
            sysfile: str = '/sys/devices/system/cpu/{}/cpufreq/scaling_max_freq'.format(cpu)
            await self.target.write_value.asyn(sysfile, value)
        except ValueError:
            raise ValueError('Frequency must be an integer; got: "{}"'.format(frequency))

    @asyn.asyncf
    async def set_governor_for_cpus(self, cpus: List[Union[str, int]], governor: str, **kwargs) -> None:
        """
        Set the governor for the specified list of CPUs.
        See https://www.kernel.org/doc/Documentation/cpu-freq/governors.txt

        :param cpus: The list of CPU for which the governor is to be set.
        """
        await self.target.async_manager.concurrently(
            self.set_governor(cpu, governor, **kwargs)
            for cpu in sorted(set(cpus))
        )

    @asyn.asyncf
    async def set_frequency_for_cpus(self, cpus: List[Union[int, str]], freq: int, exact: bool = False) -> None:
        """
        Set the frequency for the specified list of CPUs.
        See https://www.kernel.org/doc/Documentation/cpu-freq/governors.txt

        :param cpus: The list of CPU for which the frequency has to be set.
        """
        await self.target.async_manager.concurrently(
            self.set_frequency(cpu, freq, exact)
            for cpu in sorted(set(cpus))
        )

    @asyn.asyncf
    async def set_all_frequencies(self, freq: int) -> None:
        """
        Set the specified (minimum) frequency for all the (online) CPUs
        """
        # pylint: disable=protected-access
        return await self.target._execute_util.asyn(
            'cpufreq_set_all_frequencies {}'.format(freq),
            as_root=True)

    @asyn.asyncf
    async def get_all_frequencies(self) -> Dict[str, str]:
        """
        Get the current frequency for all the (online) CPUs
        """
        # pylint: disable=protected-access
        output: str = await self.target._execute_util.asyn(
            'cpufreq_get_all_frequencies', as_root=True)
        frequencies: Dict[str, str] = {}
        for x in output.splitlines():
            kv = x.split(' ')
            if kv[0] == '':
                break
            frequencies[kv[0]] = kv[1]
        return frequencies

    @asyn.asyncf
    async def set_all_governors(self, governor: str) -> None:
        """
        Set the specified governor for all the (online) CPUs
        """
        try:
            # pylint: disable=protected-access
            return await self.target._execute_util.asyn(
                'cpufreq_set_all_governors {}'.format(governor),
                as_root=True)
        except TargetStableError as e:
            if ("echo: I/O error" in str(e) or
               "write error: Invalid argument" in str(e)):

                cpus_unsupported: List[int] = [c for c in await self.target.list_online_cpus.asyn()
                                               if governor not in await self.list_governors.asyn(c)]
                raise TargetStableError("Governor {} unsupported for CPUs {}".format(
                    governor, cpus_unsupported))
            else:
                raise

    @asyn.asyncf
    async def get_all_governors(self):
        """
        Get the current governor for all the (online) CPUs
        """
        # pylint: disable=protected-access
        output = await self.target._execute_util.asyn(
            'cpufreq_get_all_governors', as_root=True)
        governors = {}
        for x in output.splitlines():
            kv = x.split(' ')
            if kv[0] == '':
                break
            governors[kv[0]] = kv[1]
        return governors

    @asyn.asyncf
    async def trace_frequencies(self):
        """
        Report current frequencies on trace file
        """
        # pylint: disable=protected-access
        return await self.target._execute_util.asyn('cpufreq_trace_all_frequencies', as_root=True)

    @asyn.asyncf
    async def get_affected_cpus(self, cpu: Union[str, int]) -> List[int]:
        """
        Get the online CPUs that share a frequency domain with the given CPU
        """
        if isinstance(cpu, int):
            cpu = 'cpu{}'.format(cpu)

        sysfile = '/sys/devices/system/cpu/{}/cpufreq/affected_cpus'.format(cpu)

        content = await self.target.read_value.asyn(sysfile)
        return [int(c) for c in content.split()]

    @asyn.asyncf
    @asyn.memoized_method
    async def get_related_cpus(self, cpu: Union[str, int]) -> List[int]:
        """
        Get the CPUs that share a frequency domain with the given CPU
        """
        if isinstance(cpu, int):
            cpu = 'cpu{}'.format(cpu)

        sysfile = '/sys/devices/system/cpu/{}/cpufreq/related_cpus'.format(cpu)

        return [int(c) for c in cast(str, (await self.target.read_value.asyn(sysfile))).split()]

    @asyn.asyncf
    @asyn.memoized_method
    async def get_driver(self, cpu: Union[str, int]) -> str:
        """
        Get the name of the driver used by this cpufreq policy.
        """
        if isinstance(cpu, int):
            cpu = 'cpu{}'.format(cpu)

        sysfile = '/sys/devices/system/cpu/{}/cpufreq/scaling_driver'.format(cpu)

        return cast(str, (await self.target.read_value.asyn(sysfile))).strip()

    @asyn.asyncf
    async def iter_domains(self) -> AsyncGenerator[Set[int], None]:
        """
        Iterate over the frequency domains in the system
        """
        cpus = set(range(self.target.number_of_cpus))
        while cpus:
            cpu = next(iter(cpus))  # pylint: disable=stop-iteration-return
            domain: Set[int] = await cast(CpufreqModule, self.target.cpufreq).get_related_cpus.asyn(cpu)
            yield domain
            cpus = cpus.difference(domain)
