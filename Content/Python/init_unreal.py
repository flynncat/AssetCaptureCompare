"""Entry point discovered by Unreal's Python plugin."""
import re
import unreal

if "-ACCWorker=" in unreal.SystemLibrary.get_command_line():
    from acc import worker
    worker.start()
else:
    from acc import editor
    editor.install()
