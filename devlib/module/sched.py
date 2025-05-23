#    Copyright 2018-2025 ARM Limited
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

import logging
import re

from devlib.module import Module
from devlib.utils.misc import memoized, get_logger
from devlib.utils.types import boolean
from devlib.exception import TargetStableError
from typing import (TYPE_CHECKING, cast, Match, Dict,
                    Any, List, Union, Optional,
                    Tuple, Set)
if TYPE_CHECKING:
    from devlib.target import Target


class SchedProcFSNode(object):
    """
    Represents a sched_domain procfs node

    :param nodes: Dictionnary view of the underlying procfs nodes
        (as returned by devlib.read_tree_values())


    Say you want to represent this path/data:
    $ cat /proc/sys/kernel/sched_domain/cpu0/domain*/name
    MC
    DIE

    Taking cpu0 as a root, this can be defined as:
    >>> data = {"domain0" : {"name" : "MC"}, "domain1" : {"name" : "DIE"}}

    >>> repr = SchedProcFSNode(data)
    >>> print repr.domains[0].name
    MC

    The "raw" dict remains available under the `procfs` field:
    >>> print repr.procfs["domain0"]["name"]
    MC
    """

    _re_procfs_node = re.compile(r"(?P<name>.*\D)(?P<digits>\d+)$")

    PACKABLE_ENTRIES: List[str] = [
        "cpu",
        "domain",
        "group"
    ]

    @staticmethod
    def _ends_with_digits(node: str) -> bool:
        """
        returns True if the node ends with digits
        """
        if not isinstance(node, str):
            return False

        return re.search(SchedProcFSNode._re_procfs_node, node) is not None

    @staticmethod
    def _node_digits(node: str) -> int:
        """
        :returns: The ending digits of the procfs node
        """
        return int(cast(Match, re.search(SchedProcFSNode._re_procfs_node, node)).group("digits"))

    @staticmethod
    def _node_name(node: str) -> str:
        """
        :returns: The name of the procfs node
        """
        match = re.search(SchedProcFSNode._re_procfs_node, node)
        if match:
            return match.group("name")

        return node

    @classmethod
    def _packable(cls, node: str) -> bool:
        """
        :returns: Whether it makes sense to pack a node into a common entry
        """
        return (SchedProcFSNode._ends_with_digits(node) and
                SchedProcFSNode._node_name(node) in cls.PACKABLE_ENTRIES)

    @staticmethod
    def _build_directory(node_name: str,
                         node_data: Any) -> Union['SchedDomain', 'SchedProcFSNode']:
        """
        create a new sched domain or a new procfs node
        """
        if node_name.startswith("domain"):
            return SchedDomain(node_data)
        else:
            return SchedProcFSNode(node_data)

    @staticmethod
    def _build_entry(node_data: Any) -> Union[int, Any]:
        value = node_data

        # Most nodes just contain numerical data, try to convert
        try:
            value = int(value)
        except ValueError:
            pass

        return value

    @staticmethod
    def _build_node(node_name: str, node_data: Any) -> Union['SchedDomain', 'SchedProcFSNode',
                                                             int, Any]:
        if isinstance(node_data, dict):
            return SchedProcFSNode._build_directory(node_name, node_data)
        else:
            return SchedProcFSNode._build_entry(node_data)

    def __getattr__(self, name: str):
        return self._dyn_attrs[name]

    def __init__(self, nodes: Dict[str, 'SchedProcFSNode']):
        self.procfs = nodes
        # First, reduce the procs fields by packing them if possible
        # Find which entries can be packed into a common entry
        packables: Dict[str, str] = {
            node: SchedProcFSNode._node_name(node) + "s"
            for node in list(cast(SchedProcFSNode, nodes).keys()) if SchedProcFSNode._packable(node)
        }

        self._dyn_attrs: Dict[str, Any] = {}

        for dest in set(packables.values()):
            self._dyn_attrs[dest] = {}

        # Pack common entries
        for key, dest in packables.items():
            i: int = SchedProcFSNode._node_digits(key)
            self._dyn_attrs[dest][i] = self._build_node(key, nodes[key])

        # Build the other nodes
        for key in nodes.keys():
            if key in packables:
                continue

            self._dyn_attrs[key] = self._build_node(key, nodes[key])


class _SchedDomainFlag:
    """
    Backward-compatible emulation of the former :class:`enum.Enum` that will
    work on recent kernels with dynamic sched domain flags name and no value
    exposed.
    """

    _INSTANCES: Dict['_SchedDomainFlag', '_SchedDomainFlag'] = {}
    """
    Dictionary storing the instances so that they can be compared with ``is``
    operator.
    """
    name: str
    _value: Optional[int]

    def __new__(cls, name: str, value: Optional[int], doc: Optional[str] = None):
        self = super().__new__(cls)
        self.name = name
        self._value = value
        self.__doc__ = doc
        return cls._INSTANCES.setdefault(self, self)

    def __eq__(self, other):
        # We *have to* check for "value" as well, otherwise it will be
        # impossible to keep in the same set 2 instances with differing values.
        return self.name == other.name and self._value == other._value

    def __hash__(self):
        return hash((self.name, self._value))

    @property
    def value(self) -> Optional[int]:
        value = self._value
        if value is None:
            raise AttributeError('The kernel does not expose the sched domain flag values')
        else:
            return value

    @staticmethod
    def check_version(target: 'Target', logger: logging.Logger) -> None:
        """
        Check the target and see if its kernel version matches our view of the world
        """
        parts: Tuple[Optional[int], Optional[int], Optional[int]] = target.kernel_version.parts
        # Checked to be valid from v4.4
        # Not saved as a class attribute else it'll be converted to an enum
        ref_parts: Tuple[int, int, int] = (4, 4, 0)
        if parts < ref_parts:
            logger.warn(
                "Sched domain flags are defined for kernels v{} and up, "
                "but target is running v{}".format(ref_parts, parts)
            )

    def __str__(self):
        return self.name

    def __repr__(self):
        return '<SchedDomainFlag: {}>'.format(self.name)


class _SchedDomainFlagMeta(type):
    """
    Metaclass of :class:`SchedDomainFlag`.

    Provides some level of emulation of :class:`enum.Enum` behavior for
    backward compatibility.
    """
    @property
    def _flags(self) -> List[Any]:
        return [
            attr
            for name, attr in self.__dict__.items()
            if name.startswith('SD_')
        ]

    def __getitem__(self, i):
        return self._flags[i]

    def __len__(self):
        return len(self._flags)

    # These would be provided by collections.abc.Sequence, but using it on a
    # metaclass seems to have issues around __init_subclass__
    def __iter__(self):
        return iter(self._flags)

    def __reversed__(self):
        return reversed(self._flags)

    def __contains__(self, x):
        return x in self._flags

    @property
    def __members__(self):
        return {flag.name: flag for flag in self._flags}


class SchedDomainFlag(_SchedDomainFlag, metaclass=_SchedDomainFlagMeta):
    """
    Represents a sched domain flag.

    .. note:: ``SD_*`` class attributes are deprecated, new code should never
        test a given flag against one of these attributes with ``is`` (.e.g ``x
        is SchedDomainFlag.SD_LOAD_BALANCE``. This is because the
        ``SD_LOAD_BALANCE`` flag exists in two flavors that are not equal: one
        with a value (the class attribute) and one without (dynamically created
        when parsing flags for new kernels). Old code ran on old kernels should
        work fine though.
    """
    # pylint: disable=bad-whitespace
    # Domain flags obtained from include/linux/sched/topology.h on v4.17
    # https://kernel.googlesource.com/pub/scm/linux/kernel/git/torvalds/linux/+/v4.17/include/linux/sched/topology.h#20
    SD_LOAD_BALANCE =        _SchedDomainFlag("SD_LOAD_BALANCE", 0x0001, "Do load balancing on this domain")
    SD_BALANCE_NEWIDLE =     _SchedDomainFlag("SD_BALANCE_NEWIDLE", 0x0002, "Balance when about to become idle")
    SD_BALANCE_EXEC =        _SchedDomainFlag("SD_BALANCE_EXEC", 0x0004, "Balance on exec")
    SD_BALANCE_FORK =        _SchedDomainFlag("SD_BALANCE_FORK", 0x0008, "Balance on fork, clone")
    SD_BALANCE_WAKE =        _SchedDomainFlag("SD_BALANCE_WAKE", 0x0010, "Balance on wakeup")
    SD_WAKE_AFFINE =         _SchedDomainFlag("SD_WAKE_AFFINE", 0x0020, "Wake task to waking CPU")
    SD_ASYM_CPUCAPACITY =    _SchedDomainFlag("SD_ASYM_CPUCAPACITY", 0x0040, "Groups have different max cpu capacities")
    SD_SHARE_CPUCAPACITY =   _SchedDomainFlag("SD_SHARE_CPUCAPACITY", 0x0080, "Domain members share cpu capacity")
    SD_SHARE_POWERDOMAIN =   _SchedDomainFlag("SD_SHARE_POWERDOMAIN", 0x0100, "Domain members share power domain")
    SD_SHARE_PKG_RESOURCES = _SchedDomainFlag("SD_SHARE_PKG_RESOURCES", 0x0200, "Domain members share cpu pkg resources")
    SD_SERIALIZE =           _SchedDomainFlag("SD_SERIALIZE", 0x0400, "Only a single load balancing instance")
    SD_ASYM_PACKING =        _SchedDomainFlag("SD_ASYM_PACKING", 0x0800, "Place busy groups earlier in the domain")
    SD_PREFER_SIBLING =      _SchedDomainFlag("SD_PREFER_SIBLING", 0x1000, "Prefer to place tasks in a sibling domain")
    SD_OVERLAP =             _SchedDomainFlag("SD_OVERLAP", 0x2000, "Sched_domains of this level overlap")
    SD_NUMA =                _SchedDomainFlag("SD_NUMA", 0x4000, "Cross-node balancing")
    # Only defined in Android
    # https://android.googlesource.com/kernel/common/+/android-4.14/include/linux/sched/topology.h#29
    SD_SHARE_CAP_STATES =    _SchedDomainFlag("SD_SHARE_CAP_STATES", 0x8000, "(Android only) Domain members share capacity state")


class SchedDomain(SchedProcFSNode):
    """
    Represents a sched domain as seen through procfs
    """
    def __init__(self, nodes: Dict[str, SchedProcFSNode]):
        super().__init__(nodes)

        flags: Union[Set[_SchedDomainFlag], str] = self.flags
        # Recent kernels now have a space-separated list of flags instead of a
        # packed bitfield
        if isinstance(flags, str):
            flags = {
                _SchedDomainFlag(name=name, value=None)
                for name in flags.split()
            }
        else:
            def has_flag(flags: Set[_SchedDomainFlag], flag: _SchedDomainFlag):
                return any(f.value == flag.value for f in flags)

            flags = {
                flag
                for flag in SchedDomainFlag
                if has_flag(flags, flag)
            }

        self.flags = flags


def _select_path(target: 'Target', paths: List[str], name: str) -> str:
    """
    select existing file path
    """
    for p in paths:
        if target.file_exists(p):
            return p

    raise TargetStableError('No {} found. Tried: {}'.format(name, ', '.join(paths)))


class SchedProcFSData(SchedProcFSNode):
    """
    Root class for creating & storing SchedProcFSNode instances
    """
    _read_depth: int = 6

    @classmethod
    def get_data_root(cls, target: 'Target'):
        # Location differs depending on kernel version
        paths = ['/sys/kernel/debug/sched/domains/', '/proc/sys/kernel/sched_domain']
        return _select_path(target, paths, "sched_domain debug directory")

    @staticmethod
    def available(target: 'Target') -> bool:
        """
        check availability of sched domains
        """
        try:
            path = SchedProcFSData.get_data_root(target)
        except TargetStableError:
            return False

        cpus: List[str] = target.list_directory(path, as_root=target.is_rooted)
        if not cpus:
            return False

        # Even if we have a CPU entry, it can be empty (e.g. hotplugged out)
        # Make sure some data is there
        for cpu in cpus:
            if target.file_exists(target.path.join(path, cpu, "domain0", "flags")):
                return True

        return False

    def __init__(self, target: 'Target', path: Optional[str] = None):
        if path is None:
            path = SchedProcFSData.get_data_root(target)

        procfs: Dict[str, 'SchedProcFSNode'] = target.read_tree_values(path, depth=self._read_depth)
        super(SchedProcFSData, self).__init__(procfs)


class SchedModule(Module):
    """
    scheduler module
    """
    name: str = 'sched'

    cpu_sysfs_root: str = '/sys/devices/system/cpu'

    @staticmethod
    def probe(target: 'Target') -> bool:
        logger: logging.Logger = get_logger(SchedModule.name)
        SchedDomainFlag.check_version(target, logger)

        # It makes sense to load this module if at least one of those
        # functionalities is enabled
        schedproc: bool = SchedProcFSData.available(target)
        debug: bool = SchedModule.target_has_debug(target)
        dmips: bool = any([target.file_exists(SchedModule.cpu_dmips_capacity_path(target, cpu))
                           for cpu in target.list_online_cpus()])

        logger.info("Scheduler sched_domain procfs entries %s",
                    "found" if schedproc else "not found")
        logger.info("Detected kernel compiled with SCHED_DEBUG=%s",
                    "y" if debug else "n")
        logger.info("CPU capacity sysfs entries %s",
                    "found" if dmips else "not found")

        return schedproc or debug or dmips

    def __init__(self, target: 'Target'):
        super().__init__(target)

    @classmethod
    def get_sched_features_path(cls, target: 'Target') -> str:
        # Location differs depending on kernel version
        paths: List[str] = ['/sys/kernel/debug/sched/features', '/sys/kernel/debug/sched_features']
        return _select_path(target, paths, "sched_features file")

    def get_kernel_attributes(self, matching: Optional[str] = None,
                              check_exit_code: bool = True) -> Dict[str, Union[int, bool]]:
        """
        Get the value of scheduler attributes.

        :param matching: an (optional) substring to filter the scheduler
        attributes to be returned.

        The scheduler exposes a list of tunable attributes under:
            /proc/sys/kernel
        all starting with the "sched_" prefix.

        This method returns a dictionary of all the "sched_" attributes exposed
        by the target kernel, within the prefix removed.
        It's possible to restrict the list of attributes by specifying a
        substring to be matched.

        returns: a dictionary of scheduler tunables
        """
        command = 'sched_get_kernel_attributes {}'.format(
            matching if matching else ''
        )
        output: str = self.target._execute_util(command, as_root=self.target.is_rooted,
                                                check_exit_code=check_exit_code)
        result: Dict[str, Union[int, bool]] = {}
        for entry in output.strip().split('\n'):
            if ':' not in entry:
                continue
            path, value_s = entry.strip().split(':', 1)
            if value_s in ['0', '1']:
                value: Union[int, bool] = bool(int(value_s))
            elif value_s.isdigit():
                value = int(value_s)
            result[path] = value
        return result

    def set_kernel_attribute(self, attr: str, value: Union[bool, int, str],
                             verify: bool = True) -> None:
        """
        Set the value of a scheduler attribute.

        :param attr: the attribute to set, without the "sched_" prefix
        :param value: the value to set
        :param verify: true to check that the requested value has been set

        :raise TargetError: if the attribute cannot be set
        """
        if isinstance(value, bool):
            value = '1' if value else '0'
        elif isinstance(value, int):
            value = str(value)
        path: str = '/proc/sys/kernel/sched_' + attr
        self.target.write_value(path, value, verify)

    @classmethod
    def target_has_debug(cls, target: 'Target') -> bool:
        """
        True if target has SCHED_DEBUG config set and has sched features
        """
        if target.config.get('SCHED_DEBUG') != 'y':
            return False

        try:
            cls.get_sched_features_path(target)
            return True
        except TargetStableError:
            return False

    def get_features(self) -> Dict[str, bool]:
        """
        Get the status of each sched feature

        :returns: a dictionary of features and their "is enabled" status
        """
        feats: str = self.target.read_value(self.get_sched_features_path(self.target))
        features: Dict[str, bool] = {}
        for feat in feats.split():
            value: bool = True
            if feat.startswith('NO'):
                feat = feat.replace('NO_', '', 1)
                value = False
            features[feat] = value
        return features

    def set_feature(self, feature: str, enable: bool, verify: bool = True):
        """
        Set the status of a specified scheduler feature

        :param feature: the feature name to set
        :param enable: true to enable the feature, false otherwise

        :raise ValueError: if the specified enable value is not bool
        :raise RuntimeError: if the specified feature cannot be set
        """
        feature = feature.upper()
        feat_value: str = feature
        if not boolean(enable):
            feat_value = 'NO_' + feat_value
        self.target.write_value(self.get_sched_features_path(self.target),
                                feat_value, verify=False)
        if not verify:
            return
        msg: str = 'Failed to set {}, feature not supported?'.format(feat_value)
        features: Dict[str, bool] = self.get_features()
        feat_ = features.get(feature, not enable)
        if feat_ != enable:
            raise RuntimeError(msg)

    def get_cpu_sd_info(self, cpu: int) -> SchedProcFSData:
        """
        :returns: An object view of the sched_domain debug directory of 'cpu'
        """
        path = self.target.path.join(
            SchedProcFSData.get_data_root(self.target),
            "cpu{}".format(cpu)
        )

        return SchedProcFSData(self.target, path)

    def get_sd_info(self) -> SchedProcFSData:
        """
        :returns: An object view of the entire sched_domain debug directory
        """
        return SchedProcFSData(self.target)

    def get_capacity(self, cpu: int) -> int:
        """
        :returns: The capacity of 'cpu'
        """
        return self.get_capacities()[cpu]

    @memoized
    def has_em(self, cpu: int, sd: Optional[SchedProcFSData] = None) -> bool:
        """
        :returns: Whether energy model data is available for 'cpu'
        """
        if not sd:
            sd = self.get_cpu_sd_info(cpu)

        return sd.procfs["domain0"].get("group0", {}).get("energy", {}).get("cap_states") is not None

    @classmethod
    def cpu_dmips_capacity_path(cls, target: 'Target', cpu: int):
        """
        :returns: The target sysfs path where the dmips capacity data should be
        """
        return target.path.join(
            cls.cpu_sysfs_root,
            'cpu{}/cpu_capacity'.format(cpu))

    @memoized
    def has_dmips_capacity(self, cpu: int) -> bool:
        """
        :returns: Whether dmips capacity data is available for 'cpu'
        """
        return self.target.file_exists(
            self.cpu_dmips_capacity_path(self.target, cpu)
        )

    @memoized
    def get_em_capacity(self, cpu: int, sd: Optional[SchedProcFSData] = None) -> int:
        """
        :returns: The maximum capacity value exposed by the EAS energy model
        """
        if not sd:
            sd = self.get_cpu_sd_info(cpu)

        cap_states: str = sd.domains[0].groups[0].energy.cap_states
        cap_states_list: List[str] = cap_states.split('\t')
        num_cap_states: int = sd.domains[0].groups[0].energy.nr_cap_states
        max_cap_index: int = -1 * int(len(cap_states_list) / num_cap_states)
        return int(cap_states_list[max_cap_index])

    @memoized
    def get_dmips_capacity(self, cpu: int) -> int:
        """
        :returns: The capacity value generated from the capacity-dmips-mhz DT entry
        """
        return self.target.read_value(
            self.cpu_dmips_capacity_path(self.target, cpu), int
        )

    def get_capacities(self, default: Optional[int] = None) -> Dict[int, int]:
        """
        :param default: Default capacity value to find if no data is
        found in procfs

        :returns: a dictionnary of the shape {cpu : capacity}

        :raises RuntimeError: Raised when no capacity information is
        found and 'default' is None
        """
        cpus: List[int] = self.target.list_online_cpus()

        capacities: Dict[int, int] = {}

        for cpu in cpus:
            if self.has_dmips_capacity(cpu):
                capacities[cpu] = self.get_dmips_capacity(cpu)

        missing_cpus: Set[int] = set(cpus).difference(capacities.keys())
        if not missing_cpus:
            return capacities

        if not SchedProcFSData.available(self.target):
            if default is not None:
                capacities.update({cpu: cast(int, default) for cpu in missing_cpus})
                return capacities
            else:
                raise RuntimeError(
                    'No capacity data for cpus {}'.format(sorted(missing_cpus)))

        sd_info: SchedProcFSData = self.get_sd_info()
        for cpu in missing_cpus:
            if self.has_em(cpu, sd_info.cpus[cpu]):
                capacities[cpu] = self.get_em_capacity(cpu, sd_info.cpus[cpu])
            else:
                if default is not None:
                    capacities[cpu] = cast(int, default)
                else:
                    raise RuntimeError('No capacity data for cpu{}'.format(cpu))

        return capacities

    @memoized
    def get_hz(self) -> int:
        """
        :returns: The scheduler tick frequency on the target
        """
        return int(cast(str, self.target.config.get('CONFIG_HZ', strict=True)))
