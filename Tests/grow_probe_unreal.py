"""Enlarge only the disposable probe's geometry to invalidate historical framing."""
from pathlib import Path
import unreal

root = Path(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_dir())).resolve()
assert root.name == "ACCCleanAcceptance"
mesh = unreal.load_asset("/Game/ACCProbe/ProbeCube.ProbeCube")
build = unreal.EditorStaticMeshLibrary.get_lod_build_settings(mesh, 0)
build.set_editor_property("build_scale3d", unreal.Vector(4, 4, 4))
unreal.EditorStaticMeshLibrary.set_lod_build_settings(mesh, 0, build)
unreal.EditorAssetLibrary.save_loaded_asset(mesh, only_if_is_dirty=False)
