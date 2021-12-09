# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Implementation of GlobalDeviceArray."""

from collections import defaultdict, Counter
import dataclasses
import numpy as np
from typing import Callable, Sequence, Tuple, Union, Mapping, Optional, List, Dict

from jax.experimental import maps
from jax import core
from jax._src.lib import xla_bridge as xb
from jax._src.lib import xla_client as xc
from jax.interpreters import pxla, xla
from jax._src.util import prod, safe_zip
from jax._src.api import device_put
from jax.interpreters.sharded_jit import PartitionSpec

Shape = Tuple[int, ...]
MeshAxes = Sequence[Union[str, Tuple[str], None]]
DeviceArray = xc.Buffer
Device = xc.Device
ArrayLike = Union[np.ndarray, DeviceArray]
Index = Tuple[slice, ...]


@dataclasses.dataclass(frozen=True)
class _HashableIndex:
  val: Index

  def __hash__(self):
    return hash(tuple([(v.start, v.stop, v.step) for v in self.val]))

  def __eq__(self, other):
    return self.val == other.val


def get_shard_indices(global_shape: Shape, global_mesh: pxla.Mesh,
                      mesh_axes: MeshAxes) -> Mapping[Device, Index]:
  # Import here to avoid cyclic import error when importing gda in pjit.py.
  from jax.experimental.pjit import get_array_mapping, _prepare_axis_resources

  if not isinstance(mesh_axes, PartitionSpec):
    pspec = PartitionSpec(*mesh_axes)
  else:
    pspec = mesh_axes
  parsed_pspec, _, _ = _prepare_axis_resources(pspec, "mesh_axes")
  array_mapping = get_array_mapping(parsed_pspec)
  # The dtype doesn't matter for creating sharding specs.
  aval = core.ShapedArray(global_shape, np.float32)
  sharding_spec = pxla.mesh_sharding_specs(
      global_mesh.shape, global_mesh.axis_names)(aval, array_mapping)
  indices = pxla.spec_to_indices(global_shape, sharding_spec)
  for index in indices:
    assert isinstance(index, tuple)
    for idx in index:
      assert isinstance(idx, slice)
  # The type: ignore is to ignore the type returned by `spec_to_indices`.
  return dict(
      (d, i)
      for d, i in safe_zip(global_mesh.devices.flat, indices))  # type: ignore


def get_shard_shape(global_shape, global_mesh, mesh_axes) -> Shape:
  chunk_size = []
  for mesh_axis, size in zip(mesh_axes, global_shape):
    if not mesh_axis:
      chunk_size.append(size)
    elif isinstance(mesh_axis, tuple):
      m = prod([global_mesh.shape[ma] for ma in mesh_axis])
      chunk_size.append(size // m)
    else:
      chunk_size.append(size // global_mesh.shape[mesh_axis])
  if len(chunk_size) != len(global_shape):
    chunk_size.extend(global_shape[len(chunk_size):])
  return tuple(chunk_size)


@dataclasses.dataclass(frozen=True)
class Shard:
  """A single data shard of a GlobalDeviceArray.

  Attributes:
    device: Which device this shard resides on.
    index: The index into the global array of this shard.
    replica_id: Integer id indicating which replica of the global array this
      shard is part of. Always `0` for fully sharded data
      (i.e. when there’s only 1 replica).
    data: The data of this shard. None if `device` is non-local.
  """
  device: Device
  index: Index
  replica_id: int
  # None if this `Shard` lives on a non-local device.
  data: Optional[DeviceArray] = None


class GlobalDeviceArray:
  """A logical array with data sharded across multiple devices and processes.

  If you’re not already familiar with JAX’s multi-process programming model,
  please read https://jax.readthedocs.io/en/latest/multi_process.html.

  A GlobalDeviceArray (GDA) can be thought of as a view into a single logical
  array sharded across processes. The logical array is the “global” array, and
  each process has a GlobalDeviceArray object referring to the same global array
  (similarly to how each process runs a multi-process pmap or pjit). Each process
  can access the shape, dtype, etc. of the global array via the GDA, pass the
  GDA into multi-process pjits, and get GDAs as pjit outputs (coming soon: xmap
  and pmap). However, each process can only directly access the shards of the
  global array data stored on its local devices.

  GDAs can help manage the inputs and outputs of multi-process computations.
  A GDA keeps track of which shard of the global array belongs to which device,
  and provides callback-based APIs to materialize the correct shard of the data
  needed for each local device of each process.

  A GDA consists of data shards. Each shard is stored on a different device.
  There are local shards and global shards. Local shards are those on local
  devices, and the data is visible to the current process. Global shards are
  those across all devices (including local devices), and the data isn’t visible
  if the shard is on a non-local device with respect to the current process.
  Please see the `Shard` class to see what information is stored inside that
  data structure.

  Note: to make pjit output GlobalDeviceArrays, set the environment variable
  `JAX_PARALLEL_FUNCTIONS_OUTPUT_GDA=true` or add the following to your code:
  `jax.config.update('jax_parallel_functions_output_gda', True)`

  Attributes:
    shape: The global shape of the array.
    dtype: dtype of the global array.
    local_shards: List of `Shard`s on the local devices of the current process.
      Data is available for all local shards.
    global_shards: List of all `Shard`s of the global array. Data isn’t
      available if a shard is on a non-local device with respect to the current
      process.

  Example:

  ```python
  # Logical mesh is (hosts, devices)
  assert global_mesh.shape == {'x': 4, 'y': 8}

  global_input_shape = (64, 32)
  mesh_axes = P('x', 'y')

  # Dummy example data; in practice we wouldn't necessarily materialize global data
  # in a single process.
  global_input_data = np.arange(
      np.prod(global_input_shape)).reshape(global_input_shape)

  def get_local_data_slice(index):
    # index will be a tuple of slice objects, e.g. (slice(0, 16), slice(0, 4))
    # This method will be called per-local device from the GSDA constructor.
    return global_input_data[index]

  gda = GlobalDeviceArray.from_callback(
          global_input_shape, global_mesh, mesh_axes, get_local_data_slice)

  f = pjit(lambda x: x @ x.T, out_axis_resources = P('y', 'x'))

  with mesh(global_mesh.shape, global_mesh.axis_names):
    out = f(gda)

  print(type(out))  # GlobalDeviceArray
  print(out.shape)  # global shape == (64, 64)
  print(out.local_shards[0].data)  # Access the data on a single local device,
                                   # e.g. for checkpointing
  print(out.local_shards[0].data.shape)  # per-device shape == (8, 16)
  print(out.local_shards[0].index) # Numpy-style index into the global array that
                                   # this data shard corresponds to

  # `out` can be passed to another pjit call, out.local_shards can be used to
  # export the data to non-jax systems (e.g. for checkpointing or logging), etc.
  ```
  """

  def __init__(self, global_shape: Shape, global_mesh: pxla.Mesh,
               mesh_axes: MeshAxes, device_buffers: Sequence[DeviceArray]):
    """Constructor of GlobalDeviceArray class.

    Args:
      global_shape: The global shape of the array
      global_mesh: The global mesh representing devices across multiple
        processes.
      mesh_axes: A sequence with length less than or equal to the rank of the
      global array (i.e. the length of the global shape). Each element can be:
        * An axis name of `global_mesh`, indicating that the corresponding
          global array axis is partitioned across the given device axis of
          `global_mesh`.
        * A tuple of axis names of `global_mesh`. This is like the above option
          except the global array axis is partitioned across the product of axes
          named in the tuple.
        * None indicating that the corresponding global array axis is not
          partitioned.
        For more information, please see:
        https://jax.readthedocs.io/en/latest/jax-101/08-pjit.html#more-information-on-partitionspec
      device_buffers: DeviceArrays that are on the local devices of
        `global_mesh`.
    """
    self._global_shape = global_shape
    self._global_mesh = global_mesh
    self._mesh_axes = mesh_axes
    assert len(device_buffers) == len(self._global_mesh.local_devices)
    self._global_shards, self._local_shards = self._create_shards(
        device_buffers)

    ss = get_shard_shape(self._global_shape, self._global_mesh, self._mesh_axes)
    assert all(db.shape == ss for db in device_buffers), (
        f"Expected shard shape {ss} doesn't match the device buffer "
        f"shape {device_buffers[0].shape}")

    dtype = device_buffers[0].dtype
    assert all(db.dtype == dtype for db in device_buffers), (
        "Input arrays to GlobalDeviceArray must have matching dtypes, "
        f"got: {[db.dtype for db in device_buffers]}")
    self.dtype = dtype

  def __str__(self):
    return f'GlobalDeviceArray(shape={self.shape}, dtype={self.dtype})'

  def __repr__(self):
    return (f'GlobalDeviceArray(shape={self.shape}, dtype={self.dtype}, '
            f'global_mesh_shape={dict(self._global_mesh.shape)}, '
            f'mesh_axes={self._mesh_axes})')

  @property
  def shape(self) -> Shape:
    return self._global_shape

  def _create_shards(
      self, device_buffers: Sequence[DeviceArray]
  ) -> Tuple[Sequence[Shard], Sequence[Shard]]:
    indices = get_shard_indices(self._global_shape, self._global_mesh,
                                self._mesh_axes)
    device_to_buffer = dict((db.device(), db) for db in device_buffers)
    gs, ls = [], []
    index_to_replica: Dict[_HashableIndex, int] = Counter()
    for device, index in indices.items():
      h_index = _HashableIndex(index)
      replica_id = index_to_replica[h_index]
      index_to_replica[h_index] += 1
      local_shard = device.process_index == xb.process_index()
      buf = device_to_buffer[device] if local_shard else None
      sh = Shard(device, index, replica_id, buf)
      gs.append(sh)
      if local_shard:
        if sh.data is None:
          raise ValueError("Local shard's data field should not be None.")
        ls.append(sh)
    return gs, ls

  @property
  def local_shards(self) -> Sequence[Shard]:
    for s in self._local_shards:
      # Ignore the type because mypy thinks data is None but local_shards
      # cannot have data=None which is checked in `_create_shards`.
      if s.data.aval is None:  # type: ignore
        s.data.aval = core.ShapedArray(s.data.shape, s.data.dtype)  # type: ignore
    return self._local_shards

  @property
  def global_shards(self) -> Sequence[Shard]:
    for g in self._global_shards:
      if g.data is not None and g.data.aval is None:
        g.data.aval = core.ShapedArray(g.data.shape, g.data.dtype)
    return self._global_shards

  def local_data(self, index) -> DeviceArray:
    return self.local_shards[index].data

  @classmethod
  def from_callback(cls, global_shape: Shape, global_mesh: pxla.Mesh,
                    mesh_axes: MeshAxes, data_callback: Callable[[Index],
                                                                 ArrayLike]):
    indices = get_shard_indices(global_shape, global_mesh, mesh_axes)
    dbs = [
        device_put(data_callback(indices[device]), device)
        for device in global_mesh.local_devices
    ]
    return cls(global_shape, global_mesh, mesh_axes, dbs)

  @classmethod
  def from_batched_callback(cls, global_shape: Shape,
                            global_mesh: pxla.Mesh, mesh_axes: MeshAxes,
                            data_callback: Callable[[Sequence[Index]],
                                                    Sequence[ArrayLike]]):
    indices = get_shard_indices(global_shape, global_mesh, mesh_axes)
    local_indices = [indices[d] for d in global_mesh.local_devices]
    local_arrays = data_callback(local_indices)
    dbs = pxla.device_put(local_arrays, global_mesh.local_devices)
    return cls(global_shape, global_mesh, mesh_axes, dbs)

  @classmethod
  def from_batched_callback_with_devices(
      cls, global_shape: Shape, global_mesh: pxla.Mesh,
      mesh_axes: MeshAxes,
      data_callback: Callable[[Sequence[Tuple[Index, Tuple[Device, ...]]]],
                              Sequence[DeviceArray]]):
    indices = get_shard_indices(global_shape, global_mesh, mesh_axes)

    index_to_device: Mapping[_HashableIndex, List[Device]] = defaultdict(list)
    for device in global_mesh.local_devices:
      h_index = _HashableIndex(indices[device])
      index_to_device[h_index].append(device)

    cb_inp = [
        (index.val, tuple(device)) for index, device in index_to_device.items()
    ]
    dbs = data_callback(cb_inp)
    return cls(global_shape, global_mesh, mesh_axes, dbs)


core.pytype_aval_mappings[GlobalDeviceArray] = lambda x: core.ShapedArray(
    x.shape, x.dtype)
xla.pytype_aval_mappings[GlobalDeviceArray] = lambda x: core.ShapedArray(
    x.shape, x.dtype)
xla.canonicalize_dtype_handlers[GlobalDeviceArray] = pxla.identity

def _gsda_shard_arg(x, devices, indices):
  pjit_mesh = maps.thread_resources.env.physical_mesh
  if x._global_mesh != pjit_mesh:
    raise ValueError("Pjit's mesh and GDA's mesh should be equal. Got Pjit "
                     f"mesh: {pjit_mesh},\n GDA mesh: {x._global_mesh}")
  assert all(g.index == i for g, i in safe_zip(x.global_shards, indices)), (
      "Indices calculated by GDA and pjit do not match. Please file a bug "
      "on https://github.com/google/jax/issues. "
      f"Got GDA indices: {[g.index for g in x.global_shards]},\n"
      f"pjit indices: {indices}")
  return [s.data for s in x.local_shards]
pxla.shard_arg_handlers[GlobalDeviceArray] = _gsda_shard_arg


def _gsda_array_result_handler(global_aval, out_axis_resources, global_mesh):
  return lambda bufs: GlobalDeviceArray(global_aval.shape, global_mesh,
                                        out_axis_resources, bufs)
pxla.global_result_handlers[core.ShapedArray] = _gsda_array_result_handler
pxla.global_result_handlers[core.ConcreteArray] = _gsda_array_result_handler
