from __future__ import print_function

from collections import defaultdict

import json
import math
import os
import pickle
import random
import re

import numpy as np
from tqdm import tqdm

from cloudfiles import CloudFiles

from cloudvolume import CloudVolume, view
from cloudvolume.lib import Vec, Bbox, jsonify
from taskqueue import RegisteredTask

import cc3d
import DracoPy
import fastremap
import zmesh

from . import mesh_graphene_remap

def calculate_draco_quantization_bits_and_range(
  min_quantization_range, max_draco_bin_size, draco_quantization_bits=None
):
  if draco_quantization_bits is None:
    draco_quantization_bits = np.ceil(
      np.log2(min_quantization_range / max_draco_bin_size + 1)
    )
  num_draco_bins = 2 ** draco_quantization_bits - 1
  draco_bin_size = np.ceil(min_quantization_range / num_draco_bins)
  draco_quantization_range = draco_bin_size * num_draco_bins
  if draco_quantization_range < min_quantization_range + draco_bin_size:
    if draco_bin_size == max_draco_bin_size:
      return calculate_quantization_bits_and_range(
        min_quantization_range, max_draco_bin_size, draco_quantization_bits + 1
      )
    else:
      draco_bin_size = draco_bin_size + 1
      draco_quantization_range = draco_quantization_range + num_draco_bins
  return draco_quantization_bits, draco_quantization_range, draco_bin_size

class MeshTask(RegisteredTask):
  def __init__(self, shape, offset, layer_path, **kwargs):
    """
    Convert all labels in the specified bounding box into meshes
    via marching cubes and quadratic edge collapse (github.com/seung-lab/zmesh).

    Required:
      shape: (sx,sy,sz) size of task
      offset: (x,y,z) offset from (0,0,0)
      layer_path: neuroglancer/cloudvolume dataset path

    Optional:
      lod: (uint) level of detail to record these meshes at
      mip: (uint) level of the resolution pyramid to download segmentation from
      simplification_factor: (uint) try to reduce the number of triangles in the 
        mesh by this factor (but constrained by max_simplification_error)
      max_simplification_error: The maximum physical distance that
        simplification is allowed to move a triangle vertex by. 
      mesh_dir: which subdirectory to write the meshes to (overrides info file location)
      remap_table: agglomerate segmentation before meshing using { orig_id: new_id }
      generate_manifests: (bool) if it is known that the meshes generated by this 
        task will not be cropped by the bounding box, avoid needing to run a seperate
        MeshManifestTask pass by generating manifests on the spot.

      These two options are used to allow sufficient overlap for trivial mesh stitching
      between adjacent tasks.

        low_padding: (uint) expand the bounding box by this many pixels by subtracting
          this padding from the minimum point of the bounding box on all axes.
        high_padding: (uint) expand the bounding box by this many pixels adding
          this padding to the maximum point of the bounding box on all axes.

      parallel_download: (uint: 1) number of processes to use during the segmentation download
      cache_control: (str: None) specify the cache-control header when uploading mesh files
      dust_threshold: (uint: None) don't bother meshing labels strictly smaller than this number of voxels.
      encoding: (str) 'precomputed' (default) or 'draco'
      draco_compression_level: (uint: 1) only applies to draco encoding
      draco_create_metadata: (bool: False) only applies to draco encoding
      progress: (bool: False) show progress bars for meshing 
      object_ids: (list of ints) if specified, only mesh these ids
      fill_missing: (bool: False) replace missing segmentation files with zeros instead of erroring
      spatial_index: (bool: False) generate a JSON spatial index of which meshes are available in
        a given bounding box. 
      sharded: (bool: False) If True, upload all meshes together as a single pickled 
        fragment file. 
      timestamp: (int: None) (graphene only) use the segmentation existing at this
        UNIX timestamp.
    """
    super(MeshTask, self).__init__(shape, offset, layer_path, **kwargs)
    self.shape = Vec(*shape)
    self.offset = Vec(*offset)
    self.layer_path = layer_path
    self.options = {
      'cache_control': kwargs.get('cache_control', None),
      'draco_compression_level': kwargs.get('draco_compression_level', 1),
      'draco_create_metadata': kwargs.get('draco_create_metadata', False),
      'dust_threshold': kwargs.get('dust_threshold', None),
      'encoding': kwargs.get('encoding', 'precomputed'),
      'fill_missing': kwargs.get('fill_missing', False),
      'generate_manifests': kwargs.get('generate_manifests', False),
      'high_padding': kwargs.get('high_padding', 1),
      'low_padding': kwargs.get('low_padding', 0),
      'lod': kwargs.get('lod', 0),
      'max_simplification_error': kwargs.get('max_simplification_error', 40),
      'simplification_factor': kwargs.get('simplification_factor', 100),
      'mesh_dir': kwargs.get('mesh_dir', None),
      'mip': kwargs.get('mip', 0),
      'object_ids': kwargs.get('object_ids', None),
      'parallel_download': kwargs.get('parallel_download', 1),
      'progress': kwargs.get('progress', False),
      'remap_table': kwargs.get('remap_table', None),
      'spatial_index': kwargs.get('spatial_index', False),
      'sharded': kwargs.get('sharded', False),
      'timestamp': kwargs.get('timestamp', None),
      'agglomerate': kwargs.get('agglomerate', True),
      'stop_layer': kwargs.get('stop_layer', 2),
      'compress': kwargs.get('compress', 'gzip'),
    }
    supported_encodings = ['precomputed', 'draco']
    if not self.options['encoding'] in supported_encodings:
      raise ValueError('Encoding {} is not supported. Options: {}'.format(
        self.options['encoding'], ', '.join(supported_encodings)
      ))
    self._encoding_to_compression_dict = {
      'precomputed': self.options['compress'],
      'draco': False,
    }

  def execute(self):
    self._volume = CloudVolume(
      self.layer_path, self.options['mip'], bounded=False,
      parallel=self.options['parallel_download'], 
      fill_missing=self.options['fill_missing']
    )
    self._bounds = Bbox(self.offset, self.shape + self.offset)
    self._bounds = Bbox.clamp(self._bounds, self._volume.bounds)

    self.progress = bool(self.options['progress'])

    self._mesher = zmesh.Mesher(self._volume.resolution)

    # Marching cubes loves its 1vx overlaps.
    # This avoids lines appearing between
    # adjacent chunks.
    data_bounds = self._bounds.clone()
    data_bounds.minpt -= self.options['low_padding']
    data_bounds.maxpt += self.options['high_padding']

    self._mesh_dir = self.get_mesh_dir()

    if self.options['encoding'] == 'draco':
      self.draco_encoding_settings = self._compute_draco_encoding_settings()

    # chunk_position includes the overlap specified by low_padding/high_padding
    # agglomerate, timestamp, stop_layer only applies to graphene volumes, 
    # no-op for precomputed
    data = self._volume.download(
      data_bounds, 
      agglomerate=self.options['agglomerate'], 
      timestamp=self.options['timestamp'], 
      stop_layer=self.options['stop_layer']
    )

    if not np.any(data):
      return

    data = self._remove_dust(data, self.options['dust_threshold'])
    data = self._remap(data)

    if self.options['object_ids']:
      data = fastremap.mask_except(data, self.options['object_ids'], in_place=True)

    data, renumbermap = fastremap.renumber(data, in_place=True)
    renumbermap = { v:k for k,v in renumbermap.items() }
    self.compute_meshes(data, renumbermap)

  def get_mesh_dir(self):
    if self.options['mesh_dir'] is not None:
      return self.options['mesh_dir']
    elif 'mesh' in self._volume.info:
      return self._volume.info['mesh']
    else:
      raise ValueError("The mesh destination is not present in the info file.")

  def _compute_draco_encoding_settings(self):
    min_quantization_range = max((self.shape + self.options['low_padding'] + self.options['high_padding']) * self._volume.resolution)
    max_draco_bin_size = np.floor(min(self._volume.resolution) / np.sqrt(2))
    draco_quantization_bits, draco_quantization_range, draco_bin_size = \
      calculate_draco_quantization_bits_and_range(min_quantization_range, max_draco_bin_size)
    draco_quantization_origin = self.offset - (self.offset % draco_bin_size)
    return {
      'quantization_bits': draco_quantization_bits,
      'compression_level': self.options['draco_compression_level'],
      'quantization_range': draco_quantization_range,
      'quantization_origin': draco_quantization_origin,
      'create_metadata': self.options['draco_create_metadata']
    }

  def _remove_dust(self, data, dust_threshold):
    if dust_threshold:
      segids, pxct = fastremap.unique(data, return_counts=True)
      dust_segids = [ sid for sid, ct in zip(segids, pxct) if ct < int(dust_threshold) ]
      data = fastremap.mask(data, dust_segids, in_place=True)

    return data

  def _remap(self, data):
    if self.options['remap_table'] is None:
      return data 

    self.options['remap_table'] = {
      int(k): int(v) for k, v in self.options['remap_table'].items()
    }

    remap = self.options['remap_table']
    remap[0] = 0

    data = fastremap.mask_except(data, list(remap.keys()), in_place=True)
    return fastremap.remap(data, remap, in_place=True)

  def compute_meshes(self, data, renumbermap):
    data = data[:, :, :, 0].T
    self._mesher.mesh(data)
    del data

    bounding_boxes = {}
    meshes = {}

    for obj_id in tqdm(self._mesher.ids(), disable=(not self.progress), desc="Mesh"):
      remapped_id = renumbermap[obj_id]
      mesh_binary, mesh_bounds = self._create_mesh(obj_id)
      bounding_boxes[remapped_id] = mesh_bounds.to_list()
      meshes[remapped_id] = mesh_binary

    if self.options['sharded']:
      self._upload_batch(meshes, self._bounds)
    else:
      self._upload_individuals(meshes, self.options['generate_manifests'])

    if self.options['spatial_index']:
      self._upload_spatial_index(self._bounds, bounding_boxes)

  def _upload_batch(self, meshes, bbox):
    cf = CloudFiles(self.layer_path, progress=self.options['progress'])
    # Create mesh batch for postprocessing later
    cf.put(
      f"{self._mesh_dir}/{bbox.to_filename()}.frags",
      content=pickle.dumps(meshes),
      compress=self.options['compress'],
      content_type="application/python-pickle",
      cache_control=False,
    )

  def _upload_individuals(self, mesh_binaries, generate_manifests):
    cf = CloudFiles(self.layer_path)
    cf.puts(
      ( 
        (
          f"{self._mesh_dir}/{segid}:{self.options['lod']}:{self._bounds.to_filename()}", 
          mesh_binary
        ) 
        for segid, mesh_binary in mesh_binaries.items() 
      ),
      compress=self._encoding_to_compression_dict[self.options['encoding']],
      cache_control=self.options['cache_control'],
    )

    if generate_manifests:
      cf.put_jsons(
        (
          (
            f"{self._mesh_dir}/{segid}:{self.options['lod']}", 
            { "fragments": [ f"{segid}:{self.options['lod']}:{self._bounds.to_filename()}" ] }
          )
          for segid, mesh_binary in mesh_binaries.items()
        ),
        compress=None,
        cache_control=self.options['cache_control'],
      )

  def _create_mesh(self, obj_id):
    mesh = self._mesher.get_mesh(
      obj_id,
      simplification_factor=self.options['simplification_factor'],
      max_simplification_error=self.options['max_simplification_error']
    )

    self._mesher.erase(obj_id)

    resolution = self._volume.resolution
    offset = self._bounds.minpt - self.options['low_padding']
    mesh.vertices[:] += offset * resolution

    mesh_bounds = Bbox(
      np.amin(mesh.vertices, axis=0), 
      np.amax(mesh.vertices, axis=0)
    )

    if self.options['encoding'] == 'draco':
      mesh_binary = DracoPy.encode_mesh_to_buffer(
        mesh.vertices.flatten('C'), mesh.faces.flatten('C'), 
        **self.draco_encoding_settings
      )
    elif self.options['encoding'] == 'precomputed':
      mesh_binary = mesh.to_precomputed()

    return mesh_binary, mesh_bounds

  def _upload_spatial_index(self, bbox, mesh_bboxes):
    cf = CloudFiles(self.layer_path, progress=self.options['progress'])
    cf.put_json(
      f"{self._mesh_dir}/{bbox.to_filename()}.spatial",
      mesh_bboxes,
      compress=self.options['compress'],
      cache_control=False,
    )

class GrapheneMeshTask(RegisteredTask):
  def __init__(self, cloudpath, shape, offset, mip, **kwargs):
    """
    Convert all labels in the specified bounding box into meshes
    via marching cubes and quadratic edge collapse (github.com/seung-lab/zmesh).

    Required:
      shape: (sx,sy,sz) size of task
      offset: (x,y,z) offset from (0,0,0)
      cloudpath: neuroglancer/cloudvolume dataset path

    Optional:
      mip: (uint) level of the resolution pyramid to download segmentation from
      simplification_factor: (uint) try to reduce the number of triangles in the 
        mesh by this factor (but constrained by max_simplification_error)
      max_simplification_error: The maximum physical distance that
        simplification is allowed to move a triangle vertex by. 
      mesh_dir: which subdirectory to write the meshes to (overrides info file location)

      parallel_download: (uint: 1) number of processes to use during the segmentation download
      cache_control: (str: None) specify the cache-control header when uploading mesh files
      dust_threshold: (uint: None) don't bother meshing labels strictly smaller than this number of voxels.
      encoding: (str) 'precomputed' (default) or 'draco'
      draco_compression_level: (uint: 1) only applies to draco encoding
      progress: (bool: False) show progress bars for meshing 
      object_ids: (list of ints) if specified, only mesh these ids
      fill_missing: (bool: False) replace missing segmentation files with zeros instead of erroring
      timestamp: (int: None) (graphene only) use the segmentation existing at this
        UNIX timestamp.
    """
    super(GrapheneMeshTask, self).__init__(cloudpath, shape, offset, mip, **kwargs)
    self.shape = Vec(*shape)
    self.offset = Vec(*offset)
    self.mip = int(mip)
    self.cloudpath = cloudpath
    self.layer_id = 2
    self.overlap_vx = 1
    self.options = {
      'cache_control': kwargs.get('cache_control', None),
      'draco_compression_level': kwargs.get('draco_compression_level', 1),
      'fill_missing': kwargs.get('fill_missing', False),
      'max_simplification_error': kwargs.get('max_simplification_error', 40),
      'simplification_factor': kwargs.get('simplification_factor', 100),
      'mesh_dir': kwargs.get('mesh_dir', None),
      'progress': kwargs.get('progress', False),
      'timestamp': kwargs.get('timestamp', None),
    }

  def execute(self):
    self.cv = CloudVolume(
      self.cloudpath, mip=self.mip, bounded=False,
      fill_missing=self.options['fill_missing'],
      mesh_dir=self.options['mesh_dir'],
    )

    if self.cv.mesh.meta.is_sharded() == False:
      raise ValueError("The mesh sharding parameter must be defined.")

    self.bounds = Bbox(self.offset, self.shape + self.offset)
    self.bounds = Bbox.clamp(self.bounds, self.cv.bounds)

    self.progress = bool(self.options['progress'])

    self.mesher = zmesh.Mesher(self.cv.resolution)

    # Marching cubes needs 1 voxel overlap to properly 
    # stitch adjacent meshes.
    # data_bounds = self.bounds.clone()
    # data_bounds.maxpt += self.overlap_vx

    self.mesh_dir = self.get_mesh_dir()
    self.draco_encoding_settings = self.compute_draco_encoding_settings()

    chunk_pos = self.cv.meta.point_to_chunk_position(self.bounds.center(), mip=self.mip)
    
    img = mesh_graphene_remap.remap_segmentation(
      self.cv, 
      chunk_pos.x, chunk_pos.y, chunk_pos.z, 
      mip=self.mip, 
      overlap_vx=self.overlap_vx, 
      time_stamp=self.timestamp,
      progress=self.progress,
    )

    if not np.any(img):
      return

    self.upload_meshes(
      self.compute_meshes(img)
    )

  def get_mesh_dir(self):
    if self.options['mesh_dir'] is not None:
      return self.options['mesh_dir']
    elif 'mesh' in self.cv.info:
      return self.cv.info['mesh']
    else:
      raise ValueError("The mesh destination is not present in the info file.")

  def compute_meshes(self, data):
    data = data.T
    self.mesher.mesh(data)
    del data

    meshes = {}
    for obj_id in tqdm(self.mesher.ids(), disable=(not self.progress), desc="Mesh"):
      # remapped_id = component_map[obj_id]
      meshes[obj_id] = self.create_mesh(obj_id)

    return meshes

  def upload_meshes(self, meshes):
    if len(meshes) == 0:
      return

    reader = self.cv.mesh.readers[self.layer_id] 

    shard_binary = reader.spec.synthesize_shard(meshes)
    # the shard filename is derived from the chunk position,
    # so any label inside this L2 chunk will do
    shard_filename = reader.get_filename(list(meshes.keys())[0]) 

    cf = CloudFiles(self.cv.cloudpath)
    cf.put(
      f"{self.get_mesh_dir()}/initial/{self.layer_id}/{shard_filename}",
      shard_binary,
      compress=None,
      content_type="application/octet-stream",
      cache_control="no-cache",
    )

  def create_mesh(self, obj_id):
    mesh = self.mesher.get_mesh(
      obj_id,
      simplification_factor=self.options['simplification_factor'],
      max_simplification_error=self.options['max_simplification_error']
    )

    self.mesher.erase(obj_id)
    mesh.vertices[:] += self.bounds.minpt * self.cv.resolution

    mesh_binary = DracoPy.encode_mesh_to_buffer(
      mesh.vertices.flatten('C'), mesh.faces.flatten('C'), 
      **self.draco_encoding_settings
    )

    return mesh_binary

  def compute_draco_encoding_settings(self):
    resolution = self.cv.resolution
    chunk_offset_nm = self.offset * resolution
    
    min_quantization_range = max(
      (self.shape + self.overlap_vx) * resolution
    )
    if self.cv.meta.uses_new_draco_bin_size:
      max_draco_bin_size = np.floor(min(resolution) / 2)
    else:
      max_draco_bin_size = np.floor(min(resolution) / np.sqrt(2))

    (
      draco_quantization_bits,
      draco_quantization_range,
      draco_bin_size,
    ) = calculate_draco_quantization_bits_and_range(
      min_quantization_range, max_draco_bin_size
    )
    draco_quantization_origin = chunk_offset_nm - (chunk_offset_nm % draco_bin_size)
    return {
      "quantization_bits": draco_quantization_bits,
      "compression_level": 1,
      "quantization_range": draco_quantization_range,
      "quantization_origin": draco_quantization_origin,
      "create_metadata": True,
    }

class MeshManifestTask(RegisteredTask):
  """
  Finalize mesh generation by post-processing chunk fragment
  lists into mesh fragment manifests.
  These are necessary for neuroglancer to know which mesh
  fragments to download for a given segid.

  If we parallelize using prefixes single digit prefixes ['0','1',..'9'] all meshes will
  be correctly processed. But if we do ['10','11',..'99'] meshes from [0,9] won't get
  processed and need to be handle specifically by creating tasks that will process
  a single mesh ['0:','1:',..'9:']
  """

  def __init__(self, layer_path, prefix, lod=0, mesh_dir=None):
    super(MeshManifestTask, self).__init__(layer_path, prefix)
    self.layer_path = layer_path
    self.lod = lod
    self.prefix = prefix
    self.mesh_dir = mesh_dir

  def execute(self):
    cf = CloudFiles(self.layer_path)
    self._info = cf.get_json('info')

    if self.mesh_dir is None and 'mesh' in self._info:
      self.mesh_dir = self._info['mesh']

    self._generate_manifests(cf)

  def _get_mesh_filenames_subset(self, cf):
    prefix = '{}/{}'.format(self.mesh_dir, self.prefix)
    segids = defaultdict(list)

    for filename in cf.list(prefix=prefix):
      filename = os.path.basename(filename)
      # `match` implies the beginning (^). `search` matches whole string
      matches = re.search(r'(\d+):(\d+):', filename)

      if not matches:
        continue

      segid, lod = matches.groups()
      segid, lod = int(segid), int(lod)

      if lod != self.lod:
        continue

      segids[segid].append(filename)

    return segids

  def _generate_manifests(self, cf):
    segids = self._get_mesh_filenames_subset(cf)
    items = ( (
        f"{self.mesh_dir}/{segid}:{self.lod}",
        json.dumps({ "fragments": frags })
      ) for segid, frags in segids.items() )

    cf.puts(items, content_type='application/json')
