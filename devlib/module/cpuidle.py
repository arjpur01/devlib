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
# pylint: disable=attribute-defined-outside-init

from operator import attrgetter
from pprint import pformat

from devlib.module import Module
from devlib.exception import TargetStableError
from devlib.utils.types import integer, boolean
from devlib.utils.misc import memoized
import devlib.utils.asyn as asyn
from typing import Optional, TYPE_CHECKING, Union, List
if TYPE_CHECKING:
    from devlib.target import Target


class CpuidleState(object):

    @property
    def usage(self) -> int:
        return integer(self.get('usage'))

    @property
    def time(self) -> int:
        return integer(self.get('time'))

    @property
    def is_enabled(self) -> bool:
        return not boolean(self.get('disable'))

    @property
    def ordinal(self) -> int:
        i = len(self.id)
        while self.id[i - 1].isdigit():
            i -= 1
            if not i:
                raise ValueError('invalid idle state name: "{}"'.format(self.id))
        return int(self.id[i:])

    def __init__(self, target: 'Target', index: int, path: str, name: str,
                 desc: str, power: int, latency: int, residency: Optional[int]):
        self.target = target
        self.index = index
        self.path = path
        self.name = name
        self.desc = desc
        self.power = power
        self.latency = latency
        self.residency = residency
        self.id: str = self.target.path.basename(self.path)
        self.cpu: str = self.target.path.basename(self.target.path.dirname(path))

    @asyn.asyncf
    async def enable(self) -> None:
        """
        enable idle state
        """
        await self.set.asyn('disable', 0)

    @asyn.asyncf
    async def disable(self) -> None:
        """
        disable idle state
        """
        await self.set.asyn('disable', 1)

    @asyn.asyncf
    async def get(self, prop: str) -> str:
        """
        get the property
        """
        property_path = self.target.path.join(self.path, prop)
        return await self.target.read_value.asyn(property_path)

    @asyn.asyncf
    async def set(self, prop: str, value: str) -> None:
        """
        set the property
        """
        property_path = self.target.path.join(self.path, prop)
        await self.target.write_value.asyn(property_path, value)

    def __eq__(self, other):
        if isinstance(other, CpuidleState):
            return (self.name == other.name) and (self.desc == other.desc)
        elif isinstance(other, str):
            return (self.name == other) or (self.desc == other)
        else:
            return False

    def __ne__(self, other):
        return not self.__eq__(other)

    def __str__(self):
        return 'CpuidleState({}, {})'.format(self.name, self.desc)

    __repr__ = __str__


class Cpuidle(Module):
    """
    ``cpuidle`` is the kernel subsystem for managing CPU low power (idle) states.
    """
    name = 'cpuidle'
    root_path = '/sys/devices/system/cpu/cpuidle'

    @staticmethod
    @asyn.asyncf
    async def probe(target: 'Target') -> bool:
        return await target.file_exists.asyn(Cpuidle.root_path)

    def __init__(self, target: 'Target'):
        super(Cpuidle, self).__init__(target)

        basepath: str = '/sys/devices/system/cpu/'
        # FIXME - annotating the values_tree based on read_tree_values return type is causing errors due to recursive
        # definition of the Node type. leaving it out for now
        values_tree = self.target.read_tree_values(basepath, depth=4, check_exit_code=False)

        self._states = {
            cpu_name: sorted(
                (
                    CpuidleState(
                        self.target,
                        # state_name is formatted as "state42"
                        index=int(state_name[len('state'):]),
                        path=self.target.path.join(basepath, cpu_name, 'cpuidle', state_name),
                        name=state_node['name'],
                        desc=state_node['desc'],
                        power=int(state_node['power']),
                        latency=int(state_node['latency']),
                        residency=int(state_node['residency']) if 'residency' in state_node else None,
                    )
                    for state_name, state_node in cpu_node['cpuidle'].items()
                    if state_name.startswith('state')
                ),
                key=attrgetter('index'),
            )

            for cpu_name, cpu_node in values_tree.items()
            if cpu_name.startswith('cpu') and 'cpuidle' in cpu_node
        }

        self.logger.debug('Adding cpuidle states:\n{}'.format(pformat(self._states)))

    def get_states(self, cpu: Union[int, str] = 0) -> List[CpuidleState]:
        """
        get the cpu idle states
        """
        if isinstance(cpu, int):
            cpu = 'cpu{}'.format(cpu)
        return self._states.get(cpu, [])

    def get_state(self, state: Union[str, int], cpu: Union[str, int] = 0) -> CpuidleState:
        """
        get the specific cpuidle state values
        """
        if isinstance(state, int):
            try:
                return self.get_states(cpu)[state]
            except IndexError:
                raise ValueError('Cpuidle state {} does not exist'.format(state))
        else:  # assume string-like
            for s in self.get_states(cpu):
                if state in [s.id, s.name, s.desc]:
                    return s
            raise ValueError('Cpuidle state {} does not exist'.format(state))

    @asyn.asyncf
    async def enable(self, state: Union[str, int], cpu: Union[str, int] = 0) -> None:
        """
        enable the specific cpu idle state
        """
        await self.get_state(state, cpu).enable.asyn()

    @asyn.asyncf
    async def disable(self, state: Union[str, int], cpu: Union[str, int] = 0) -> None:
        """
        disable the specific cpu idle state
        """
        await self.get_state(state, cpu).disable.asyn()

    @asyn.asyncf
    async def enable_all(self, cpu: Union[str, int] = 0) -> None:
        """
        enable all the cpu idle states
        """
        await self.target.async_manager.concurrently(
            state.enable.asyn()
            for state in self.get_states(cpu)
        )

    @asyn.asyncf
    async def disable_all(self, cpu: Union[str, int] = 0) -> None:
        """
        disable all cpu idle states
        """
        await self.target.async_manager.concurrently(
            state.disable.asyn()
            for state in self.get_states(cpu)
        )

    @asyn.asyncf
    async def perturb_cpus(self) -> None:
        """
        Momentarily wake each CPU. Ensures cpu_idle events in trace file.
        """
        # pylint: disable=protected-access
        await self.target._execute_util.asyn('cpuidle_wake_all_cpus')

    @asyn.asyncf
    async def get_driver(self) -> str:
        """
        get the current driver of idle states
        """
        return await self.target.read_value.asyn(self.target.path.join(self.root_path, 'current_driver'))

    @memoized
    def list_governors(self) -> List[str]:
        """Returns a list of supported idle governors."""
        sysfile: str = self.target.path.join(self.root_path, 'available_governors')
        output: str = self.target.read_value(sysfile)
        return output.strip().split()

    @asyn.asyncf
    async def get_governor(self) -> str:
        """Returns the currently selected idle governor."""
        path = self.target.path.join(self.root_path, 'current_governor_ro')
        if not await self.target.file_exists.asyn(path):
            path = self.target.path.join(self.root_path, 'current_governor')
        return await self.target.read_value.asyn(path)

    def set_governor(self, governor: str) -> None:
        """
        Set the idle governor for the system.

        :param governor: The name of the governor to be used. This must be
        supported by the specific device.

        :raises TargetStableError if governor is not supported by the CPU, or
        if, for some reason, the governor could not be set.
        """
        supported: List[str] = self.list_governors()
        if governor not in supported:
            raise TargetStableError('Governor {} not supported'.format(governor))
        sysfile: str = self.target.path.join(self.root_path, 'current_governor')
        self.target.write_value(sysfile, governor)
