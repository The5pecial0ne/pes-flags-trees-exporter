bl_info = {
    "name": "PES Flags & Trees",
    "author": "PallasDav (original v0.2.0d logic); ported and extended by DAV",
    "version": (1, 0, 0),
    "blender": (2, 80, 0),
    "location": "Properties > Scene > PES Flags & Trees",
    "description": "Add, edit, export and re-import animated corner-flags and trees for PES stadium mods — split out as a standalone tool, independent of PES Lightmanager.",
    "warning": "",
    "wiki_url": "",
    "category": "Import-Export",
    "show_expanded": True,
}

import bpy
import os
import sys
import re
import shutil
import struct
import subprocess
import xml.etree.ElementTree as ET

from bpy_extras.io_utils import ImportHelper
from bpy.types import Operator, Panel, PropertyGroup, UIList
from bpy.props import StringProperty, BoolProperty, IntProperty, FloatProperty, CollectionProperty, PointerProperty, EnumProperty

def load_template(filename):
    template_path = os.path.join(os.path.dirname(__file__), "templates", filename)
    with open(template_path, 'r') as file:
        return file.read()

def find_runtime_executable(name):
    """Locate a runtime executable (mono/wine) even when running inside a
    GUI-launched app like Blender.app, which does NOT inherit the PATH set
    up by shell rc files (.zprofile's `brew shellenv` etc). shutil.which()
    alone misses Homebrew/Mono-installer binaries in that case, so we also
    probe the well-known install locations directly."""
    found = shutil.which(name)
    if found:
        return found

    known_locations = {
        'mono': [
            "/opt/homebrew/bin/mono",                                    # Homebrew, Apple Silicon
            "/usr/local/bin/mono",                                       # Homebrew, Intel
            "/Library/Frameworks/Mono.framework/Versions/Current/Commands/mono",  # official Mono .pkg installer
        ],
        'wine': [
            "/opt/homebrew/bin/wine",
            "/usr/local/bin/wine",
            "/opt/homebrew/bin/wine64",
            "/usr/local/bin/wine64",
        ],
    }
    for candidate in known_locations.get(name, []):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None

def safe_preview_ensure(image):
    """Force-generate an image's UI preview thumbnail, tolerating Blender
    versions where Image.preview_ensure() doesn't exist (e.g. 2.93 — this
    method was added in a later Blender release). Never raises."""
    if image is None:
        return
    try:
        if hasattr(image, 'preview_ensure'):
            image.preview_ensure()
    except Exception as e:
        print(f"[LightManager] preview_ensure() unavailable/failed for {getattr(image, 'name', '?')}: {e}")

def build_tool_command(tool_path, *extra_args):
    """Build the subprocess argv for running one of the bundled Windows/.NET
    console tools (GzsTool.exe, FoxTool.exe, FtexTool.exe).

    These ship as Mono/.NET PE32 assemblies. On Windows they can be exec'd
    directly. On macOS/Linux the kernel cannot execute a PE binary at all,
    no matter its Unix permission bits — it needs to be run through a CLR
    runtime. We prefer `mono` (lighter, exactly matches "Mono/.Net assembly"
    per `file`), falling back to `wine` if mono isn't available.
    """
    if sys.platform.startswith('win'):
        return [tool_path] + list(extra_args)

    mono_path = find_runtime_executable('mono')
    if mono_path:
        # These tools' underlying data (PES archive formats) embeds
        # Windows-style backslash-separated relative paths. Real Windows
        # .NET treats '\' as a path separator when building output paths;
        # Mono on macOS/Linux correctly does NOT (it isn't one on Unix),
        # so without help these tools create single files whose *name* is
        # the whole backslash-joined string instead of nested folders.
        # MONO_IOMAP=all is Mono's own compatibility shim for exactly this
        # — it maps '\' to '/' at the I/O layer transparently. Setting it
        # in this process's environment is enough; subprocess.run inherits
        # the parent environment by default, so every tool invocation
        # picks it up without touching each call site individually.
        os.environ['MONO_IOMAP'] = 'all'
        return [mono_path, tool_path] + list(extra_args)

    wine_path = find_runtime_executable('wine')
    if wine_path:
        return [wine_path, tool_path] + list(extra_args)

    raise RuntimeError(
        f"Neither 'mono' nor 'wine' could be found (checked PATH and common "
        f"Homebrew/Mono-installer locations) — required to run "
        f"{os.path.basename(tool_path)} on this platform. If you've already "
        f"installed Mono via Homebrew, note that Blender.app launched from "
        f"Finder/Dock doesn't see your Terminal's PATH — try quitting Blender "
        f"and launching it from Terminal instead (`open -a Blender`), or "
        f"install Mono via the official .pkg installer from "
        f"https://www.mono-project.com/download/stable/ which installs to a "
        f"fixed location this addon also checks."
    )
STADIUMMODEL_TEMPLATE = load_template("stadiummodel_template.xml")


# Flag/tree "dynamic object" variants ported from PES Lightmanager v0.2.0d.
# objectType matches the old tool's numeric scheme exactly (kept for on-disk
# compatibility with any objects/files produced by that version).
# create_scale / export_scale_mult are inverses of each other by design: the
# viewport scale compensates for each .fmdl variant having different authored
# mesh dimensions, while the exported transform_scale always normalizes back
# to the same ~0.7 baseline regardless of which size variant was picked.
FLAG_VARIANTS = {
    'FLAG_A': {
        'label': "Waving Flag (Medium)",
        'name_prefix': "VA_FLAG",
        'obj_file': "flagmodel.obj",
        'object_type': 0,
        'model_prefix': "standsFlagA",
        'create_scale': 0.7,
        'export_scale_mult': 1.0,
        'addr_base': 300000,
        'transform_base': 310000,
    },
    'FLAG_C': {
        'label': "Waving Flag (Large)",
        'name_prefix': "VA_FLAG",
        'obj_file': "flagmodel.obj",
        'object_type': 1,
        'model_prefix': "standsFlagC",
        'create_scale': 0.7 * 1.5,
        'export_scale_mult': 1.0 / 1.5,
        'addr_base': 300000,
        'transform_base': 310000,
    },
    'FLAG_D': {
        'label': "Waving Flag (Small)",
        'name_prefix': "VA_FLAG",
        'obj_file': "flagmodel.obj",
        'object_type': 2,
        'model_prefix': "standsFlagD",
        'create_scale': 0.7 / 1.2,
        'export_scale_mult': 1.2,
        'addr_base': 300000,
        'transform_base': 310000,
    },
}

TREE_VARIANTS = {
    'TREE_A0': {
        'label': "Tree (Type a0)",
        'name_prefix': "VA_TREE_A0",
        'obj_file': os.path.join("va_tree", "va_tree_a0.obj"),
        'model_file': "/Assets/pes16/model/bg/common/cornerflag/va_tree001_a0.fmdl",
        'addr_base': 400000,
        'transform_base': 410000,
        'texture_map': {
            "cm059_h_tree018": "tree_tex_001.dds",
            "cm004_bg_tree002_cmem": "tree_tex_002.dds",
            "cm059_h_tree017": "tree_tex_003.dds",
        },
    },
    'TREE_B0': {
        'label': "Tree (Type b0)",
        'name_prefix': "VA_TREE_B0",
        'obj_file': os.path.join("va_tree", "va_tree_b0.obj"),
        'model_file': "/Assets/pes16/model/bg/common/cornerflag/va_tree001_b0.fmdl",
        'addr_base': 500000,
        'transform_base': 510000,
        'texture_map': {
            "cm059_h_tree018": "tree_tex_001.dds",
            "cm004_bg_tree002_cmem": "tree_tex_002.dds",
            "cm059_h_tree017": "tree_tex_003.dds",
        },
    },
}

def install_pil():
    """Check if PIL is available. Required for the Flags/Trees export pipeline
    (resizing custom flag textures to a valid in-game size). Auto-install is
    still disabled here — if missing, the export step reports a clear error
    telling the user to run `pip install Pillow` for Blender's Python."""
    try:
        import PIL
        return True, "PIL already available", None
    except ImportError:
        return False, "PIL not available (required for exporting custom flag textures)", None

# Only check — no install attempt
PIL_AVAILABLE, PIL_MESSAGE, PYTHON_EXE = install_pil()
if PIL_AVAILABLE:
    from PIL import Image
else:
    Image = None


class PIL_PT_installation_panel(bpy.types.Panel):
    bl_label = "PIL Installation Required"
    bl_idname = "PIL_PT_flags_trees_installation"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "PES Flags & Trees"

    @classmethod
    def poll(cls, context):
        # This addon has no tab system of its own — just show whenever
        # Pillow is missing, since the export step needs it regardless.
        return not PIL_AVAILABLE
    
    def draw(self, context):
        layout = self.layout
        
        layout.label(text="PIL/Pillow Required", icon='ERROR')
        layout.separator()
        
        # Show what happened during auto-install
        box = layout.box()
        box.label(text="Auto-Installation Status:")
        box.label(text=PIL_MESSAGE, icon='INFO')
        
        layout.separator()
        layout.label(text="Manual Installation Instructions:", icon='TOOL_SETTINGS')
        
        # Method 1: Command Line
        box = layout.box()
        box.label(text="Method 1: Command Line", icon='CONSOLE')
        box.label(text="1. Close Blender completely")
        box.label(text="2. Open Command Prompt/Terminal as Administrator")
        box.label(text="3. Copy and run this command:")
        
        if PYTHON_EXE:
            install_cmd = f'"{PYTHON_EXE}" -m pip install Pillow'
        else:
            install_cmd = f'"{sys.executable}" -m pip install Pillow'
        
        # Split long commands for better display
        if len(install_cmd) > 50:
            box.label(text=install_cmd[:50] + "...")
            box.label(text="   " + install_cmd[50:])
        else:
            box.label(text=install_cmd)
        
        row = box.row()
        row.operator("wm.console_toggle", text="Open Console", icon='CONSOLE')
        row.operator("pes_flags_trees.copy_install_command", text="Copy Command", icon='COPYDOWN')
        
        box.label(text="4. Restart Blender")
        
        layout.separator()
        
        # Method 2: Blender Console (Advanced)
        box = layout.box()
        box.label(text="Method 2: Blender Console (Advanced)", icon='SCRIPTPLUGINS')
        box.label(text="1. Go to Scripting workspace")
        box.label(text="2. In the console, paste and run:")
        box.label(text="import subprocess, sys")
        box.label(text="subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'Pillow'])")
        box.label(text="3. Restart Blender")
        
        layout.separator()
        
        # Troubleshooting
        box = layout.box()
        box.label(text="Troubleshooting:", icon='QUESTION')
        box.label(text="• Run Command Prompt as Administrator")
        box.label(text="• Check internet connection")
        box.label(text="• Try: python -m pip install --user Pillow")
        box.label(text="• Restart Blender after installation")
        
        layout.separator()
        
        # Retry and Help buttons
        row = layout.row()
        row.operator("pes_flags_trees.retry_pil_install", text="Retry Auto-Install", icon='FILE_REFRESH')
        row.operator("wm.url_open", text="Get Help", icon='HELP').url = "https://pillow.readthedocs.io/en/stable/installation.html"

class PES_OT_copy_install_command(bpy.types.Operator):
    bl_idname = "pes_flags_trees.copy_install_command"
    bl_label = "Copy Install Command"
    bl_description = "Copy the PIL installation command to clipboard"
    
    def execute(self, context):
        if PYTHON_EXE:
            install_cmd = f'"{PYTHON_EXE}" -m pip install Pillow'
        else:
            install_cmd = f'"{sys.executable}" -m pip install Pillow'
        
        context.window_manager.clipboard = install_cmd
        self.report({'INFO'}, "Command copied to clipboard")
        return {'FINISHED'}

class PES_OT_retry_pil_install(bpy.types.Operator):
    bl_idname = "pes_flags_trees.retry_pil_install"
    bl_label = "Retry PIL Installation"
    bl_description = "Attempt to install PIL/Pillow again"
    
    def execute(self, context):
        global PIL_AVAILABLE, PIL_MESSAGE, PYTHON_EXE, Image
        
        self.report({'INFO'}, "Attempting to install PIL/Pillow...")
        
        PIL_AVAILABLE, PIL_MESSAGE, PYTHON_EXE = install_pil()
        
        if PIL_AVAILABLE:
            from PIL import Image
            self.report({'INFO'}, "PIL/Pillow installed successfully! Please restart Blender.")
            
            # Force redraw of areas to hide the installation panel
            for area in context.screen.areas:
                area.tag_redraw()
        else:
            self.report({'ERROR'}, f"Installation failed: {PIL_MESSAGE}")
        
        return {'FINISHED'}

class TextureMapEntry(bpy.types.PropertyGroup):
    tex_id: bpy.props.StringProperty()
    file_name: bpy.props.StringProperty()
    file_path: bpy.props.StringProperty()
    modified: bpy.props.BoolProperty()
    new: bpy.props.BoolProperty()
    image: bpy.props.PointerProperty(type=bpy.types.Image)

class FLAG_UL_List(UIList):
    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        search_lower = context.scene.flag_search_query.lower()

        flags = [
            self.bitflag_filter_item if search_lower in item.key.lower() or search_lower in item.addr.lower() else 0
            for item in items
        ]

        return flags, []

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            if item.obj_type == 'Locator':
                icon = 'OUTLINER_OB_EMPTY'
            elif item.obj_type == 'LocatorPlus':
                icon = 'EMPTY_ARROWS'
            else:
                icon = 'OBJECT_DATA'

            addr_display = item.addr  # No need to format, just use the string directly

            row = layout.row()
            row.label(text="", icon=icon)
            if item.new:
                row.label(text="", icon='KEYTYPE_EXTREME_VEC')
            elif item.modified and item.renamed:
                row.label(text="", icon='KEYTYPE_JITTER_VEC')
            elif item.modified:
                row.label(text="", icon='KEYTYPE_BREAKDOWN_VEC')

            row.label(text=f"{item.key}: {addr_display}")

            if context.scene.flag_list_index == index:
                obj = bpy.data.objects.get(item.key)
                if obj and obj.type == 'EMPTY':
                    bpy.context.view_layer.objects.active = obj
                    obj.select_set(True)
        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text="", icon=icon)

class FlagListItem(PropertyGroup):
    key: StringProperty()
    original_key: StringProperty()
    addr: StringProperty()  # Change this from IntProperty to StringProperty
    obj_type: StringProperty()
    modified: BoolProperty(default=False)
    renamed: BoolProperty(default=False)
    new: BoolProperty(default=False)
    flag_type: EnumProperty(
        items=[
            ('VA_FLAG', 'VA_FLAG', 'Standard flag'),
            ('VA_TREE', 'VA_TREE', 'Tree flag'),
            ('OTHER', 'OTHER', 'Other entry'),
        ],
        name="Flag Type"
    )

class PES_OT_add_new_texture_slot(Operator):
    bl_idname = "pes_flags_trees.add_new_texture_slot"
    bl_label = "Add New Texture Slot"
    bl_description = "Create a new available texture slot"

    def execute(self, context):
        scene = context.scene
        
        # Find the next available texture ID
        existing_ids = set()
        for item in scene.pes_texture_map:
            try:
                existing_ids.add(int(item.tex_id))
            except ValueError:
                continue
        
        # Find the next available ID (0000-9999)
        next_id = None
        for i in range(10000):  # 0000 to 9999
            if i not in existing_ids:
                next_id = i
                break
        
        if next_id is None:
            self.report({'ERROR'}, "No available texture slots (maximum 10000 reached)")
            return {'CANCELLED'}
        
        # Format ID as 4-digit string
        tex_id = f"{next_id:04d}"
        
        # Get target path
        target_path = scene.pes_texture_target_path
        if not target_path or not os.path.exists(target_path):
            self.report({'ERROR'}, "Target texture path not found. Import an FPKD first.")
            return {'CANCELLED'}
        
        # Create placeholder files
        dds_filename = f"{tex_id}_bsm.dds"
        ftex_filename = f"{tex_id}_bsm.ftex"
        dds_path = os.path.join(target_path, dds_filename)
        ftex_path = os.path.join(target_path, ftex_filename)
        
        try:
            # Copy default texture as placeholder
            addon_dir = os.path.dirname(__file__)
            default_texture_dir = os.path.join(addon_dir, "resources-peslightmanager", "Objects", "va_flag_001", "textures")
            
            # Find a default texture to copy
            default_dds = None
            for file in os.listdir(default_texture_dir):
                if file.endswith('_bsm.dds'):
                    default_dds = os.path.join(default_texture_dir, file)
                    break
            
            if not default_dds:
                self.report({'ERROR'}, "No default texture found to create placeholder")
                return {'CANCELLED'}
            
            # Copy default texture as new slot
            shutil.copy2(default_dds, dds_path)
            
            # Copy or create FTEX
            default_ftex = default_dds.replace('.dds', '.ftex')
            if os.path.exists(default_ftex):
                shutil.copy2(default_ftex, ftex_path)
            else:
                # Create FTEX using FtexTool
                ftextool_path = os.path.join(addon_dir, "resources-peslightmanager", "FtexTool", "FtexTool.exe")
                if os.path.exists(ftextool_path):
                    cmd = build_tool_command(ftextool_path, "-f", "0", dds_path)
                    subprocess.run(cmd, check=True, capture_output=True)
            
            print(f"Created new texture slot files: {dds_filename}, {ftex_filename}")
            
        except Exception as e:
            self.report({'ERROR'}, f"Failed to create texture files: {str(e)}")
            return {'CANCELLED'}
        
        # Add to texture map
        item = scene.pes_texture_map.add()
        item.tex_id = tex_id
        item.file_name = dds_filename
        item.file_path = dds_path
        item.modified = False
        item.new = True  # Mark as new
        
        # Load image
        try:
            # Remove existing image with same name if it exists
            existing_image = bpy.data.images.get(dds_filename)
            if existing_image:
                bpy.data.images.remove(existing_image)
            
            image = bpy.data.images.load(dds_path)
            image.use_fake_user = True
            image.name = dds_filename
            safe_preview_ensure(image)
            bpy.context.view_layer.update()
            item.image = image
            
        except Exception as e:
            print(f"Failed to load image for new texture slot: {str(e)}")
            item.image = None
        
        # Select the new texture
        scene.pes_texture_map_index = len(scene.pes_texture_map) - 1
        
        # Force UI redraw
        for area in context.screen.areas:
            if area.type == 'PROPERTIES':
                area.tag_redraw()
        
        self.report({'INFO'}, f"Created new texture slot: {tex_id}")
        return {'FINISHED'}

class TEXTURE_UL_List(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            
            if item.image and item.image.preview:
                icon_value = item.image.preview.icon_id
            else:
                icon_value = 0
            
            row.operator("pes_flags_trees.select_texture", text="", icon_value=icon_value, emboss=True).index = index
            row.prop(item, "tex_id", text="", emboss=False)
            row.prop(item, "file_name", text="", emboss=False)
            
            if item.modified:
                row.label(text="", icon='RADIOBUT_ON')
            else:
                row.label(text="", icon='RADIOBUT_OFF')
                
        elif self.layout_type in {'GRID'}:
            # Use a box to contain everything and make previews very large
            layout.alignment = 'CENTER'
            
            # Create main container
            main_col = layout.column(align=True)
            
            # Very large preview area
            if item.image and hasattr(item.image, 'preview') and item.image.preview and item.image.preview.icon_id:
                # Create a large square button for the preview
                preview_box = main_col.box()
                preview_col = preview_box.column(align=True)
                
                # Make the preview button fill most of the space
                for i in range(4):  # Create multiple rows to make it taller
                    preview_row = preview_col.row()
                    preview_row.scale_y = 2.0
                    if i == 1:  # Only put the operator on the middle row
                        op = preview_row.operator("pes_flags_trees.select_texture", 
                                                text="", 
                                                icon_value=item.image.preview.icon_id, 
                                                emboss=False)
                        op.index = index
                    else:
                        preview_row.label(text="")  # Empty space
                        
            else:
                # Fallback for missing images
                preview_box = main_col.box()
                preview_col = preview_box.column(align=True)
                
                for i in range(4):
                    preview_row = preview_col.row()
                    preview_row.scale_y = 2.0
                    if i == 1:
                        op = preview_row.operator("pes_flags_trees.select_texture", 
                                                text="", 
                                                icon='TEXTURE', 
                                                emboss=False)
                        op.index = index
                    else:
                        preview_row.label(text="")
            
            # Small ID label at the bottom
            id_row = main_col.row()
            id_row.alignment = 'CENTER'
            id_row.scale_y = 0.5
            id_row.label(text=f"ID: {item.tex_id}")

class PES_OT_select_texture(bpy.types.Operator):
    bl_idname = "pes_flags_trees.select_texture"
    bl_label = "Select Texture"
    bl_description = "Select this texture slot"

    index: bpy.props.IntProperty()

    def execute(self, context):
        scene = context.scene
        
        # Ensure the index is valid
        if 0 <= self.index < len(scene.pes_texture_map):
            scene.pes_texture_map_index = self.index
            
            # Force UI redraw to show selection
            for area in context.screen.areas:
                if area.type == 'PROPERTIES':
                    area.tag_redraw()
            
            # Print debug info
            selected_texture = scene.pes_texture_map[self.index]
            print(f"Selected texture slot: {selected_texture.tex_id}")
            
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, f"Invalid texture index: {self.index}")
            return {'CANCELLED'}

class PES_OT_edit_texture(bpy.types.Operator, ImportHelper):
    bl_idname = "pes_flags_trees.edit_texture"
    bl_label = "Edit Texture"
    bl_description = "Replace the selected texture with a new DDS file"

    filename_ext = ".dds"
    filter_glob: StringProperty(default="*.dds", options={'HIDDEN'})

    def resize_dds_top_anchor(self, input_file_path, output_file_path, x_factor=1, y_factor=1.4545, allowed_sizes=[4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096]):
        # Implement the resize_dds_top_anchor method here
        # This should be the same implementation as in your original code
        pass

    def execute(self, context):
        scene = context.scene
        selected_texture = scene.pes_texture_map[scene.pes_texture_map_index]

        # Get the target path from the scene property
        target_path = scene.pes_texture_target_path
        if not target_path:
            self.report({'ERROR'}, "Texture target path not set. Please import FPKD first.")
            return {'CANCELLED'}

        # Define the target file paths
        target_dds_file = f"{selected_texture.tex_id}_bsm.dds"
        target_dds_path = os.path.join(target_path, target_dds_file)
        target_ftex_file = f"{selected_texture.tex_id}_bsm.ftex"
        target_ftex_path = os.path.join(target_path, target_ftex_file)

        print(f"=== Starting texture replacement for ID: {selected_texture.tex_id} ===")
        print(f"Source file: {self.filepath}")
        print(f"Target DDS: {target_dds_path}")
        print(f"Target FTEX: {target_ftex_path}")

        # Backup original files if they exist
        backup_dds = target_dds_path + ".backup"
        backup_ftex = target_ftex_path + ".backup"
        
        if os.path.exists(target_dds_path):
            shutil.copy2(target_dds_path, backup_dds)
            print(f"Backed up original DDS to: {backup_dds}")
        
        if os.path.exists(target_ftex_path):
            shutil.copy2(target_ftex_path, backup_ftex)
            print(f"Backed up original FTEX to: {backup_ftex}")

        # Process the texture with Pillow before copying
        try:
            print("=== Processing texture with Pillow ===")
            processed_image = self.process_texture_with_white_bottom(self.filepath)
            
            # Save the processed image as DDS
            # Convert to RGB if RGBA (DDS doesn't always handle alpha well)
            if processed_image.mode == 'RGBA':
                # Create white background
                background = Image.new('RGB', processed_image.size, (255, 255, 255))
                background.paste(processed_image, mask=processed_image.split()[-1])  # Use alpha as mask
                processed_image = background
            elif processed_image.mode != 'RGB':
                processed_image = processed_image.convert('RGB')
            
            # Save as temporary PNG first, then convert to DDS
            temp_png_path = target_dds_path.replace('.dds', '_temp.png')
            processed_image.save(temp_png_path, 'PNG')
            print(f"Saved processed image as PNG: {temp_png_path}")
            
            # Convert PNG to DDS using Pillow
            processed_image.save(target_dds_path, 'DDS')
            print(f"Saved processed image as DDS: {target_dds_path}")
            
            # Clean up temp file
            if os.path.exists(temp_png_path):
                os.remove(temp_png_path)
                
        except Exception as e:
            self.report({'ERROR'}, f"Failed to process texture: {str(e)}")
            print(f"Texture processing error: {str(e)}")
            import traceback
            traceback.print_exc()
            return {'CANCELLED'}

        # Verify the file was created
        if not os.path.exists(target_dds_path):
            self.report({'ERROR'}, "Processed texture file wasn't created")
            return {'CANCELLED'}

        # Remove existing FTEX file before creating new one
        if os.path.exists(target_ftex_path):
            os.remove(target_ftex_path)
            print(f"Removed existing FTEX file: {target_ftex_path}")

        # Convert DDS to FTEX
        ftextool_path = os.path.join(os.path.dirname(__file__), "resources-peslightmanager", "FtexTool", "FtexTool.exe")
        if not os.path.exists(ftextool_path):
            self.report({'ERROR'}, f"FtexTool.exe not found at {ftextool_path}")
            return {'CANCELLED'}

        cmd = build_tool_command(ftextool_path, "-f", "0", target_dds_path)
        print(f"Executing command: {' '.join(cmd)}")

        try:
            result = subprocess.run(cmd, check=True, capture_output=True, encoding="utf-8", errors="replace", cwd=os.path.dirname(ftextool_path))
            print(f"FtexTool output: {result.stdout}")
            print(f"FtexTool stderr: {result.stderr}")

            # Wait a moment for file system to update
            import time
            time.sleep(0.5)

            if not os.path.exists(target_ftex_path):
                print(f"FTEX file not found at expected location: {target_ftex_path}")
                self.report({'WARNING'}, "FTEX file may not have been created, but continuing...")
            else:
                print(f"FTEX file created successfully: {target_ftex_path}")
                
        except subprocess.CalledProcessError as e:
            print(f"FtexTool failed with return code: {e.returncode}")
            print(f"FtexTool error output: {e.stderr}")
            self.report({'WARNING'}, "Failed to convert DDS to FTEX, but continuing with DDS only")

        # Update the texture information
        selected_texture.file_path = target_dds_path
        selected_texture.file_name = target_dds_file
        selected_texture.modified = True

        # Reload image and update preview (your existing code)
        try:
            print("=== Starting image reload process ===")
            
            # Remove ALL images that might be related to this texture
            images_to_remove = []
            for img in bpy.data.images:
                if (img.name == target_dds_file or 
                    img.name.startswith(selected_texture.tex_id) or
                    (img.filepath and os.path.normpath(img.filepath) == os.path.normpath(target_dds_path))):
                    images_to_remove.append(img)
            
            for img in images_to_remove:
                print(f"Removing image: {img.name}")
                bpy.data.images.remove(img)
            
            selected_texture.image = None
            
            import gc
            gc.collect()
            
            import time
            time.sleep(0.2)
            
            # Load the new processed image
            print(f"Loading new processed image from: {target_dds_path}")
            image = bpy.data.images.load(target_dds_path)
            image.use_fake_user = True
            image.name = target_dds_file
            
            print(f"Processed image loaded: {image.name}")
            print(f"Processed image size: {image.size[0]}x{image.size[1]}")
            
            safe_preview_ensure(image)
            bpy.context.view_layer.update()
            safe_preview_ensure(image)
            
            selected_texture.image = image
            
            if hasattr(image, 'preview') and image.preview:
                print(f"Preview created with icon_id: {image.preview.icon_id}")
            else:
                print("Warning: Preview not created")
                image.reload()
                safe_preview_ensure(image)
            
            print(f"Processed image successfully updated: {image.name}")
            
        except Exception as e:
            self.report({'ERROR'}, f"Failed to reload processed image: {str(e)}")
            print(f"Image reload error: {str(e)}")
            import traceback
            traceback.print_exc()
            return {'CANCELLED'}

        # Force UI refresh (your existing code)
        print("=== Forcing UI refresh ===")
        bpy.context.view_layer.update()
        
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
        
        current_frame = bpy.context.scene.frame_current
        bpy.context.scene.frame_set(current_frame)
        bpy.context.evaluated_depsgraph_get().update()

        print("=== Texture replacement completed ===")
        self.report({'INFO'}, f"Processed texture updated: {selected_texture.file_name}")
        return {'FINISHED'}

    def process_texture_with_white_bottom(self, input_path):
        """
        Process the input texture to stretch it to 68.84% of the top and add white bottom
        """
        from PIL import Image
        
        # Open the source image
        source_image = Image.open(input_path)
        print(f"Source image size: {source_image.size}")
        print(f"Source image mode: {source_image.mode}")
        
        # Get original dimensions
        original_width, original_height = source_image.size
        
        # Calculate the stretched height (68.84% of the final image)
        stretch_percentage = 0.6884
        stretched_height = int(original_height * stretch_percentage)
        
        print(f"Stretch percentage: {stretch_percentage * 100}%")
        print(f"Stretched height: {stretched_height}")
        print(f"White area height: {original_height - stretched_height}")
        
        # Create new image with same dimensions, white background
        processed_image = Image.new('RGBA', (original_width, original_height), (255, 255, 255, 255))
        
        # Resize the source image to fit in the top portion (stretch vertically)
        stretched_source = source_image.resize((original_width, stretched_height), Image.Resampling.LANCZOS)
        
        # Paste the stretched image at the top
        processed_image.paste(stretched_source, (0, 0))
        
        print(f"Final processed image size: {processed_image.size}")
        print(f"Stretched source pasted at: (0, 0) with size {stretched_source.size}")
        
        return processed_image

class PES_OT_add_new_flag(Operator):
    bl_idname = "pes_flags_trees.add_new_flag"
    bl_label = "Add New Flag"
    bl_description = "Add a new flag to the list"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        new_flag = context.scene.flag_list.add()
        new_flag.key = f"VA_FLAG_{len(context.scene.flag_list):04d}"
        new_flag.addr = 0  # You may want to set a default address
        new_flag.flag_type = 'VA_FLAG'
        new_flag.obj_type = 'Locator'
        new_flag.new = True
        context.scene.flag_list_index = len(context.scene.flag_list) - 1
        return {'FINISHED'}

def _pes_get_objects_collection(context):
    """Get/create the 'Objects' collection used for placed flags/trees,
    matching the old tool's convention, and make it active."""
    coll = bpy.data.collections.get("Objects")
    if not coll:
        coll = bpy.data.collections.new("Objects")
        context.scene.collection.children.link(coll)
    layer_coll = context.view_layer.layer_collection.children.get("Objects")
    if layer_coll:
        context.view_layer.active_layer_collection = layer_coll
    return coll


def _pes_import_obj(filepath):
    """Import a Wavefront .obj, handling both the legacy (<4.0) and current
    (>=4.0) Blender OBJ importer APIs. Returns the imported object, or None.

    NOTE: hasattr(bpy.ops.wm, "obj_import") is NOT a valid way to detect
    this — bpy.ops submodules are dynamic proxies that report True for any
    attribute name regardless of whether the operator is actually
    registered; the failure only surfaces when you call it. Use the real
    Blender version instead, with a same-call fallback as a safety net.
    """
    before = set(bpy.context.scene.objects)
    use_new_api = bpy.app.version >= (4, 0, 0)
    primary_error = None
    try:
        if use_new_api:
            bpy.ops.wm.obj_import(filepath=filepath)
        else:
            bpy.ops.import_scene.obj(filepath=filepath)
    except Exception as e:
        primary_error = e
        try:
            if use_new_api:
                bpy.ops.import_scene.obj(filepath=filepath)
            else:
                bpy.ops.wm.obj_import(filepath=filepath)
        except Exception as e2:
            print(f"[LightManager] OBJ import failed for {filepath}: {primary_error!r} (primary), {e2!r} (fallback)")
            return None
    after = set(bpy.context.scene.objects)
    new_objs = [o for o in (after - before) if o.type == 'MESH']
    if new_objs:
        return new_objs[0]
    # Fallback: some importer versions just leave the imported object selected
    return bpy.context.selected_objects[0] if bpy.context.selected_objects else None


def _pes_next_indexed_name(prefix):
    base = prefix + "_{:04d}"
    i = 0
    name = base.format(i)
    while name in bpy.data.objects:
        i += 1
        name = base.format(i)
    return name


def _pes_setup_flag_material(obj, texture_path):
    """Ensure obj's 'flag_material' slot has an Image Texture node feeding
    Base Color, loading texture_path if given. Mirrors the old tool's node
    setup so search_and_assign_texture_ids() (export side) can find it by
    the 'flag_material' slot-name prefix that ships in flagmodel.mtl."""
    material = None
    for slot in obj.material_slots:
        if slot.material and slot.material.name.startswith("flag_material"):
            material = slot.material
            break
    if material is None:
        material = obj.data.materials[0] if obj.data.materials else bpy.data.materials.new(name="flag_material")
        if not obj.data.materials:
            obj.data.materials.append(material)
        else:
            obj.data.materials[0] = material

    material.use_nodes = True
    nodes = material.node_tree.nodes

    texture_node = None
    for node in nodes:
        if node.type == 'TEX_IMAGE':
            texture_node = node
            break
    if texture_node is None:
        texture_node = nodes.new(type='ShaderNodeTexImage')
        texture_node.location = (-400, 0)

    if texture_path and os.path.isfile(texture_path):
        existing = None
        for img in bpy.data.images:
            if bpy.path.abspath(img.filepath) == bpy.path.abspath(texture_path):
                existing = img
                break
        texture_node.image = existing if existing else bpy.data.images.load(texture_path)

    shader_node = None
    for node in nodes:
        if node.type == 'BSDF_PRINCIPLED':
            shader_node = node
            break
    if shader_node is not None and texture_node.image is not None:
        links = material.node_tree.links
        for link in list(links):
            if link.to_node == shader_node and link.to_socket.name == 'Base Color':
                links.remove(link)
        links.new(texture_node.outputs["Color"], shader_node.inputs["Base Color"])


class PES_OT_add_flag(Operator):
    """Add a placed flag object (variant='FLAG_A'/'FLAG_C'/'FLAG_D')."""
    bl_idname = "pes_flags_trees.add_flag"
    bl_label = "Add Flag"
    bl_options = {'REGISTER', 'UNDO'}

    variant: StringProperty(default='FLAG_A')

    def execute(self, context):
        info = FLAG_VARIANTS.get(self.variant)
        if info is None:
            self.report({'ERROR'}, f"Unknown flag variant: {self.variant}")
            return {'CANCELLED'}

        addon_dir = os.path.dirname(__file__)
        obj_path = os.path.join(addon_dir, "resources-peslightmanager", "Objects", "va_flag_001", info['obj_file'])
        if not os.path.isfile(obj_path):
            self.report({'ERROR'}, f"Flag model not found: {obj_path}")
            return {'CANCELLED'}

        _pes_get_objects_collection(context)
        imported = _pes_import_obj(obj_path)
        if imported is None:
            self.report({'ERROR'}, "Failed to import flag model.")
            return {'CANCELLED'}

        imported.name = _pes_next_indexed_name(info['name_prefix'])
        try:
            imported.rotation_mode = 'QUATERNION'
        except AttributeError:
            pass

        # Place at the 3D cursor so the new flag lands where the user is looking
        imported.location = context.scene.cursor.location.copy()
        imported.scale = (info['create_scale'],) * 3

        default_texture = os.path.join(addon_dir, "resources-peslightmanager", "Objects", "va_flag_001", "default_flag.dds")
        _pes_setup_flag_material(imported, default_texture if os.path.isfile(default_texture) else None)

        imported["va_kind"] = self.variant
        imported["objectType"] = info['object_type']
        # Placeholder addr/transform — EXPORT_OT_flagarea_fpkd recomputes
        # these for every VA_FLAG_* object at export time (renumbering).
        imported["hex_value"] = "0x00000000"
        imported["hex_transform"] = "0x00000000"

        context.view_layer.objects.active = imported
        for o in context.selected_objects:
            o.select_set(False)
        imported.select_set(True)

        self.report({'INFO'}, f"Added {info['label']}: {imported.name}")
        return {'FINISHED'}


class PES_OT_add_tree(Operator):
    """Add a placed tree object (variant='TREE_A0'/'TREE_B0')."""
    bl_idname = "pes_flags_trees.add_tree"
    bl_label = "Add Tree"
    bl_options = {'REGISTER', 'UNDO'}

    variant: StringProperty(default='TREE_A0')

    def execute(self, context):
        info = TREE_VARIANTS.get(self.variant)
        if info is None:
            self.report({'ERROR'}, f"Unknown tree variant: {self.variant}")
            return {'CANCELLED'}

        addon_dir = os.path.dirname(__file__)
        obj_path = os.path.join(addon_dir, "resources-peslightmanager", "Objects", info['obj_file'])
        if not os.path.isfile(obj_path):
            self.report({'ERROR'}, f"Tree model not found: {obj_path}")
            return {'CANCELLED'}

        _pes_get_objects_collection(context)
        imported = _pes_import_obj(obj_path)
        if imported is None:
            self.report({'ERROR'}, "Failed to import tree model.")
            return {'CANCELLED'}

        imported.name = _pes_next_indexed_name(info['name_prefix'])
        try:
            imported.rotation_mode = 'QUATERNION'
        except AttributeError:
            pass

        imported.location = context.scene.cursor.location.copy()

        # Trees keep their fixed, baked-in bark/leaf textures — wire each
        # material slot to the matching bundled texture by material-name
        # prefix (identical to the old tool; never user-editable).
        addon_dir_textures = os.path.join(addon_dir, "resources-peslightmanager", "Objects", "va_tree")
        for slot in imported.material_slots:
            if not slot.material:
                continue
            tex_file = None
            for prefix, fname in info['texture_map'].items():
                if slot.material.name.startswith(prefix):
                    tex_file = fname
                    break
            if not tex_file:
                continue
            material = slot.material
            material.use_nodes = True
            material.blend_method = 'CLIP'
            nodes = material.node_tree.nodes
            texture_node = None
            for node in nodes:
                if node.type == 'TEX_IMAGE':
                    texture_node = node
                    break
            if texture_node is None:
                texture_node = nodes.new(type='ShaderNodeTexImage')
                texture_node.location = (-400, 0)
            tex_path = os.path.join(addon_dir_textures, tex_file)
            if os.path.isfile(tex_path):
                existing = None
                for img in bpy.data.images:
                    if bpy.path.abspath(img.filepath) == bpy.path.abspath(tex_path):
                        existing = img
                        break
                texture_node.image = existing if existing else bpy.data.images.load(tex_path)
            shader_node = None
            for node in nodes:
                if node.type == 'BSDF_PRINCIPLED':
                    shader_node = node
                    break
            if shader_node is not None and texture_node.image is not None:
                links = material.node_tree.links
                links.new(texture_node.outputs["Color"], shader_node.inputs["Base Color"])
                links.new(texture_node.outputs["Alpha"], shader_node.inputs["Alpha"])

        imported["va_kind"] = self.variant
        imported["objectType"] = self.variant  # 'TREE_A0' / 'TREE_B0' — trees have no size variant, just an id
        imported["hex_value"] = "0x00000000"
        imported["hex_transform"] = "0x00000000"

        context.view_layer.objects.active = imported
        for o in context.selected_objects:
            o.select_set(False)
        imported.select_set(True)

        self.report({'INFO'}, f"Added {info['label']}: {imported.name}")
        return {'FINISHED'}


class PES_OT_apply_flag_texture(Operator, ImportHelper):
    """Apply a custom .dds texture to the selected flag object(s).
    Trees intentionally reject this, matching the old tool's behavior."""
    bl_idname = "pes_flags_trees.apply_flag_texture"
    bl_label = "Apply Texture to Selected Flags"
    bl_description = "Apply a .dds texture to the selected flag(s). Trees cannot be textured."

    filename_ext = ".dds"
    filter_glob: StringProperty(default="*.dds", options={'HIDDEN'})

    def execute(self, context):
        selected_flags = [o for o in context.selected_objects if o.name.startswith("VA_FLAG_")]
        selected_trees = [o for o in context.selected_objects
                           if o.name.startswith("VA_TREE_A0") or o.name.startswith("VA_TREE_B0")]

        if selected_trees:
            self.report({'WARNING'}, "You cannot apply textures to trees — skipped.")

        if not selected_flags:
            self.report({'ERROR'}, "No flag objects selected.")
            return {'CANCELLED'}

        for obj in selected_flags:
            _pes_setup_flag_material(obj, self.filepath)

        self.report({'INFO'}, f"Applied texture to {len(selected_flags)} flag(s).")
        return {'FINISHED'}


class PES_OT_add_texture(bpy.types.Operator):
    bl_idname = "pes_flags_trees.add_texture"
    bl_label = "Add Texture"
    bl_description = "Add a new texture to the texture map"

    def execute(self, context):
        new_tex = context.scene.pes_texture_map.add()
        new_tex.tex_id = str(len(context.scene.pes_texture_map))
        new_tex.file_name = f"New_Texture_{new_tex.tex_id}.dds"
        new_tex.file_path = "/path/to/new/texture.dds"
        new_tex.modified = False
        new_tex.new = True

        # Load the image
        image = bpy.data.images.load(new_tex.file_path)
        image.use_fake_user = True
        new_tex.image = image

        return {'FINISHED'}

class PES_OT_remove_texture(bpy.types.Operator):
    bl_idname = "pes_flags_trees.remove_texture"
    bl_label = "Remove Texture"
    bl_description = "Remove the selected texture from the texture map and delete files from folder"

    def execute(self, context):
        scene = context.scene
        index = scene.pes_texture_map_index
        
        if index < 0 or index >= len(scene.pes_texture_map):
            self.report({'ERROR'}, "No texture selected")
            return {'CANCELLED'}
        
        item = scene.pes_texture_map[index]
        tex_id = item.tex_id
        
        print(f"=== Removing texture ID: {tex_id} ===")
        
        # Get the target path from the scene property
        target_path = scene.pes_texture_target_path
        if not target_path or not os.path.exists(target_path):
            self.report({'WARNING'}, "Texture target path not found. Files may not be deleted.")
        
        # Define the files to remove
        files_to_remove = []
        if target_path:
            dds_file = os.path.join(target_path, f"{tex_id}_bsm.dds")
            ftex_file = os.path.join(target_path, f"{tex_id}_bsm.ftex")
            backup_dds = dds_file + ".backup"
            backup_ftex = ftex_file + ".backup"
            
            files_to_remove = [dds_file, ftex_file, backup_dds, backup_ftex]
        
        # Remove files from folder
        removed_files = []
        failed_files = []
        
        for file_path in files_to_remove:
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    removed_files.append(os.path.basename(file_path))
                    print(f"Removed file: {file_path}")
                except Exception as e:
                    failed_files.append(os.path.basename(file_path))
                    print(f"Failed to remove file {file_path}: {str(e)}")
        
        # Remove the image data from Blender
        if item.image:
            try:
                image_name = item.image.name
                bpy.data.images.remove(item.image)
                print(f"Removed Blender image: {image_name}")
            except Exception as e:
                print(f"Failed to remove Blender image: {str(e)}")
        
        # Remove from texture map
        scene.pes_texture_map.remove(index)
        
        # Adjust index if necessary
        if index >= len(scene.pes_texture_map) and len(scene.pes_texture_map) > 0:
            scene.pes_texture_map_index = len(scene.pes_texture_map) - 1
        elif len(scene.pes_texture_map) == 0:
            scene.pes_texture_map_index = -1
        
        # Report results
        if removed_files:
            print(f"Successfully removed files: {', '.join(removed_files)}")
        
        if failed_files:
            self.report({'WARNING'}, f"Failed to remove some files: {', '.join(failed_files)}")
        
        if removed_files and not failed_files:
            self.report({'INFO'}, f"Texture {tex_id} removed successfully. Files deleted: {', '.join(removed_files)}")
        elif removed_files and failed_files:
            self.report({'WARNING'}, f"Texture {tex_id} removed. Some files couldn't be deleted: {', '.join(failed_files)}")
        else:
            self.report({'INFO'}, f"Texture {tex_id} removed from list (no files found to delete)")
        
        # Force UI refresh
        bpy.context.view_layer.update()
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
        
        print(f"=== Texture {tex_id} removal completed ===")
        return {'FINISHED'}

class PES_OT_import_flagarea_fpkd(Operator, ImportHelper):
    bl_idname = "pes_flags_trees.import_flagarea_fpkd"
    bl_label = "Import Flagarea FPKD"
    bl_description = "Import Flagarea FPKD file"

    filename_ext = ".fpkd"
    filter_glob: StringProperty(default="*.fpkd", options={'HIDDEN'})

    def find_folder(self, root_folder, target_folder):
        """Find a specific folder within a root directory"""
        for root, dirs, files in os.walk(root_folder):
            if target_folder in dirs:
                return os.path.join(root, target_folder)
        return None

    def find_fox2_file(self, folder):
        """Find FOX2 file in the given folder"""
        for file in os.listdir(folder):
            if file.startswith("flagarea_") and file.endswith(".fox2"):
                return os.path.join(folder, file)
        return None

    def load_default_texture(self, context):
        """Load default texture when no VA_FLAG or VA_TREE items are found"""
        try:
            # Get the target path where textures should be stored
            target_path = context.scene.pes_texture_target_path
            if not target_path or not os.path.exists(target_path):
                self.report({'ERROR'}, "Target texture path not found")
                print(f"Error: Target texture path not found or doesn't exist: {target_path}")
                return

            # Path to default texture in resources
            addon_dir = os.path.dirname(__file__)
            default_texture_dir = os.path.join(addon_dir, "resources-peslightmanager", "Objects", "va_flag_001", "textures")
            
            if not os.path.exists(default_texture_dir):
                self.report({'ERROR'}, f"Default texture directory not found: {default_texture_dir}")
                print(f"Error: Default texture directory not found: {default_texture_dir}")
                return

            # Look for a suitable default texture (preferably _bsm.dds or similar)
            default_texture_files = []
            for file in os.listdir(default_texture_dir):
                if file.endswith('.dds'):
                    default_texture_files.append(file)

            if not default_texture_files:
                self.report({'ERROR'}, "No DDS files found in default texture directory")
                print(f"Error: No DDS files found in: {default_texture_dir}")
                return

            # Prefer _bsm.dds if available, otherwise use the first DDS file
            default_texture_file = None
            for file in default_texture_files:
                if '_bsm.dds' in file:
                    default_texture_file = file
                    break
            
            if not default_texture_file:
                default_texture_file = default_texture_files[0]

            print(f"Selected default texture file: {default_texture_file}")

            source_texture_path = os.path.join(default_texture_dir, default_texture_file)
            dest_texture_path = os.path.join(target_path, "0000_bsm.dds")

            # Copy the default texture as 0000_bsm.dds
            shutil.copy2(source_texture_path, dest_texture_path)
            print(f"Copied default texture from {source_texture_path} to {dest_texture_path}")

            # Also copy corresponding FTEX if it exists
            ftex_source = source_texture_path.replace('.dds', '.ftex')
            if os.path.exists(ftex_source):
                ftex_dest = os.path.join(target_path, "0000_bsm.ftex")
                shutil.copy2(ftex_source, ftex_dest)
                print(f"Copied default FTEX from {ftex_source} to {ftex_dest}")
            else:
                # Create FTEX from DDS using FtexTool
                ftextool_path = os.path.join(os.path.dirname(__file__), "resources-peslightmanager", "FtexTool", "FtexTool.exe")
                if os.path.exists(ftextool_path):
                    try:
                        cmd = build_tool_command(ftextool_path, "-f", "0", dest_texture_path)
                        print(f"Creating FTEX using command: {' '.join(cmd)}")
                        result = subprocess.run(cmd, check=True, capture_output=True, encoding="utf-8", errors="replace")
                        print(f"Created FTEX file using FtexTool: {result.stdout}")
                    except subprocess.CalledProcessError as e:
                        print(f"Failed to create FTEX file: {e}")
                        print(f"FtexTool stderr: {e.stderr}")
                else:
                    print(f"FtexTool not found at: {ftextool_path}")

            # Clear existing texture map
            context.scene.pes_texture_map.clear()
            print("Cleared existing texture map")
            
            # Create texture map entry
            item = context.scene.pes_texture_map.add()
            item.tex_id = "0000"
            item.file_name = "0000_bsm.dds"
            item.file_path = dest_texture_path
            item.modified = False
            item.new = True  # Mark as new since it's a default texture

            print(f"Added texture map entry: ID={item.tex_id}, File={item.file_name}")

            # Load the image into Blender with proper settings
            try:
                if os.path.exists(dest_texture_path):
                    # Remove existing image with same name if it exists
                    existing_image = bpy.data.images.get("0000_bsm.dds")
                    if existing_image:
                        print("Removing existing image with same name")
                        bpy.data.images.remove(existing_image)
                    
                    # Load new image
                    print(f"Loading image from: {dest_texture_path}")
                    image = bpy.data.images.load(dest_texture_path)
                    image.use_fake_user = True
                    image.name = "0000_bsm.dds"
                    
                    # Force preview generation - this is key for UI display!
                    safe_preview_ensure(image)
                    print("Generated image preview")
                    
                    # Force Blender to update the view layer
                    bpy.context.view_layer.update()
                    
                    # Assign image to texture map item
                    item.image = image
                    print(f"Successfully loaded default texture: {item.file_name}")
                    print(f"Image dimensions: {image.size[0]}x{image.size[1]}")
                    
                    # Verify preview was created
                    if hasattr(image, 'preview') and image.preview:
                        print(f"Image preview created successfully, icon_id: {image.preview.icon_id}")
                    else:
                        print("Warning: Image preview not created")
                    
                else:
                    print(f"Error: Default texture file not found after copy: {dest_texture_path}")
                    item.image = None
                    
            except RuntimeError as e:
                print(f"RuntimeError loading default texture image: {str(e)}")
                item.image = None
            except Exception as e:
                print(f"Unexpected error loading default texture image: {str(e)}")
                import traceback
                traceback.print_exc()
                item.image = None

            # Force redraw of all relevant UI areas
            for area in context.screen.areas:
                if area.type == 'PROPERTIES':
                    area.tag_redraw()
                elif area.type == 'VIEW_3D':
                    area.tag_redraw()

            # Set texture map index to show the new texture
            context.scene.pes_texture_map_index = 0

            self.report({'INFO'}, "Default texture loaded as ID 0000")
            print("=== Default texture loading completed successfully ===")

        except Exception as e:
            error_msg = f"Failed to load default texture: {str(e)}"
            self.report({'ERROR'}, error_msg)
            print(f"ERROR: {error_msg}")
            import traceback
            print("Full traceback:")
            traceback.print_exc()

    def execute(self, context):
        # Check if the selected file matches the pattern
        file_name = os.path.basename(self.filepath)
        if not re.match(r"flagarea_st\d{3}\.fpkd", file_name):
            self.report({'ERROR'}, f"Invalid file name: {file_name}. Expected pattern: flagarea_st[000-099].fpkd")
            return {'CANCELLED'}

        # Path to GzsTool.exe
        gzstool_path = os.path.join(os.path.dirname(__file__), "resources-peslightmanager", "Gzs", "GzsTool.exe")
        if not os.path.exists(gzstool_path):
            self.report({'ERROR'}, f"GzsTool.exe not found at {gzstool_path}")
            return {'CANCELLED'}

        # Extract FPKD file
        try:
            subprocess.run(build_tool_command(gzstool_path, self.filepath), check=True)
        except subprocess.CalledProcessError:
            self.report({'ERROR'}, f"Failed to extract FPKD file: {file_name}")
            return {'CANCELLED'}

        # Find and extract corresponding FPK file
        fpk_file = self.filepath[:-1]  # Change .fpkd to .fpk
        if os.path.exists(fpk_file):
            try:
                subprocess.run(build_tool_command(gzstool_path, fpk_file), check=True)
            except subprocess.CalledProcessError:
                self.report({'ERROR'}, f"Failed to extract FPK file: {os.path.basename(fpk_file)}")
                return {'CANCELLED'}
            # Auto-fill the export path from the .fpk we just imported — it
            # lives right next to the .fpkd the user picked, so there's no
            # reason to make them browse to the same file again for export.
            context.scene.pes_flagarea_export_path = fpk_file
        else:
            self.report({'WARNING'}, f"Corresponding FPK file not found: {os.path.basename(fpk_file)}")

        # Process extracted folders
        fpkd_folder = self.filepath[:-5] + "_fpkd"
        fpk_folder = self.filepath[:-5] + "_fpk"

        # Find standsFlag folder
        stands_flag_folder = self.find_folder(fpkd_folder, "standsFlag")
        if not stands_flag_folder:
            self.report({'ERROR'}, "standsFlag folder not found in FPKD extraction")
            return {'CANCELLED'}

        # Find cornerflag folder
        corner_flag_folder = self.find_folder(fpk_folder, "cornerflag")
        if not corner_flag_folder:
            self.report({'WARNING'}, "cornerflag folder not found in FPK extraction")

        # Find and process FOX2 file
        fox2_file = self.find_fox2_file(stands_flag_folder)
        if not fox2_file:
            self.report({'ERROR'}, "No matching FOX2 file found in standsFlag folder")
            return {'CANCELLED'}

        # Path to FoxTool.exe
        foxtool_path = os.path.join(os.path.dirname(__file__), "resources-peslightmanager", "FoxTool", "FoxTool.exe")
        if not os.path.exists(foxtool_path):
            self.report({'ERROR'}, f"FoxTool.exe not found at {foxtool_path}")
            return {'CANCELLED'}

        # Extract FOX2 file to XML
        try:
            subprocess.run(build_tool_command(foxtool_path, fox2_file), check=True)
        except subprocess.CalledProcessError:
            self.report({'ERROR'}, "Failed to extract FOX2 file to XML")
            return {'CANCELLED'}

        # Process the XML file
        xml_file = fox2_file + ".xml"
        if not os.path.exists(xml_file):
            self.report({'ERROR'}, f"XML file not found: {xml_file}")
            return {'CANCELLED'}

        # Find or create the cornerflag folder
        assets_folder = self.find_folder(os.path.dirname(self.filepath), "Assets")
        if not assets_folder:
            self.report({'ERROR'}, "Assets folder not found")
            return {'CANCELLED'}

        cornerflag_path = os.path.join(assets_folder, "pes16", "model", "bg", "common", "cornerflag")
        os.makedirs(cornerflag_path, exist_ok=True)

        # Copy specific files from the package
        package_source = os.path.join(os.path.dirname(__file__), "resources-peslightmanager", "Objects", "va_flag_001")
        if not os.path.exists(package_source):
            self.report({'ERROR'}, f"Package source not found: {package_source}")
            return {'CANCELLED'}

        files_to_copy = [
            "dml_mobH_audi_flagbearer_01_mob_prop_teamflag_home01.gani",
            "mob_prop_teamflag_anim_skel.ask",
            "mob_prop_teamflag_home01.fmdl",
            "mob_prop_teamflag_home01.skl",
            "mob_prop_teamflag_render_skel.ask",
            "mob_prop_teamflag_skel.frig",
            "standsFlagA_0000.fmdl",
            "standsFlagC_0000.fmdl",
            "standsFlagD_0000.fmdl"
        ]

        try:
            for file in files_to_copy:
                src = os.path.join(package_source, file)
                dst = os.path.join(cornerflag_path, file)
                if os.path.exists(src):
                    shutil.copy2(src, dst)
                else:
                    self.report({'WARNING'}, f"File not found in package: {file}")
        except Exception as e:
            self.report({'ERROR'}, f"Failed to copy files: {str(e)}")
            return {'CANCELLED'}

        bg_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(self.filepath))))

        # Create the directory structure
        target_path = os.path.join(bg_path, "common", "standsFlag", "sourceimages", "tga", "#windx11")
        os.makedirs(target_path, exist_ok=True)

        # Store the target_path in the scene property
        context.scene.pes_texture_target_path = target_path

        # Copy all files from the textures package
        textures_source = os.path.join(os.path.dirname(__file__), "resources-peslightmanager", "Objects", "va_flag_001", "textures")
        if not os.path.exists(textures_source):
            self.report({'ERROR'}, f"Textures source not found: {textures_source}")
            return {'CANCELLED'}

        try:
            for item in os.listdir(textures_source):
                s = os.path.join(textures_source, item)
                d = os.path.join(target_path, item)
                if os.path.isfile(s):
                    shutil.copy2(s, d)
                else:
                    shutil.copytree(s, d, dirs_exist_ok=True)
            self.report({'INFO'}, f"Texture package copied to: {target_path}")
        except Exception as e:
            self.report({'ERROR'}, f"Failed to copy texture package: {str(e)}")
            return {'CANCELLED'}

        # REMOVE THE OLD TEXTURE PROCESSING CODE HERE
        # The texture map will be populated in process_xml_file -> load_all_texture_slots

        # Process XML file - this will now handle all texture loading
        self.process_xml_file(xml_file, context)

        return {'FINISHED'}

    def process_xml_file(self, xml_file, context):
        try:
            if not os.path.exists(xml_file):
                self.report({'ERROR'}, f"XML file does not exist: {xml_file}")
                print(f"Error: XML file does not exist: {xml_file}")
                return

            tree = ET.parse(xml_file)
            root = tree.getroot()

            print(f"XML file parsed successfully: {xml_file}")

            # Find the DataSet entity
            dataset_entity = root.find(".//entity[@class='DataSet']")
            if dataset_entity is None:
                self.report({'WARNING'}, "No 'DataSet' entity found in XML")
                print("Warning: DataSet entity not found in XML")
                return

            # Find the dataList property within the DataSet entity
            data_list_prop = dataset_entity.find(".//property[@name='dataList']")
            if data_list_prop is None:
                self.report({'WARNING'}, "No 'dataList' property found in DataSet entity")
                print("Warning: dataList property not found in DataSet entity")
                return

            print(f"dataList property found: {data_list_prop.attrib}")

            # Clear existing flag list
            context.scene.flag_list.clear()

            value_count = 0
            flag_count = 0
            has_va_flag_or_tree = False

            for value in data_list_prop.findall('value'):
                value_count += 1
                key = value.get('key')
                addr = value.text

                print(f"Processing value: key={key}, addr={addr}")

                # Add all entries to the flag list
                item = context.scene.flag_list.add()
                item.key = key
                item.original_key = key
                item.addr = addr

                if key.startswith('VA_FLAG'):
                    item.flag_type = 'VA_FLAG'
                    item.obj_type = 'StadiumModel'
                    has_va_flag_or_tree = True
                elif key.startswith('VA_TREE'):
                    item.flag_type = 'VA_TREE'
                    item.obj_type = 'StadiumModel'
                    has_va_flag_or_tree = True
                else:
                    item.flag_type = 'OTHER'
                    item.obj_type = 'Other'

                flag_count += 1
                print(f"Added item: {key}")

            # LOAD ALL EXISTING TEXTURE SLOTS (not just default)
            self.load_all_texture_slots(context, has_va_flag_or_tree)

            # Spawn placement objects in the viewport for every VA_FLAG_*/
            # VA_TREE_* entry so they can be seen/dragged/re-exported —
            # closes the import -> edit -> export round trip.
            self.spawn_placement_objects(context, root)

            self.report({'INFO'}, f"Processed Flag List: {len(context.scene.flag_list)} items added")
            print(f"Total values processed: {value_count}")
            print(f"Items added: {flag_count}")

            # Redraw the UI
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()

        except ET.ParseError as e:
            self.report({'ERROR'}, f"Failed to parse XML file: {xml_file}")
            print(f"ParseError: Failed to parse XML file: {xml_file}")
            print(f"Error details: {str(e)}")
        except Exception as e:
            self.report({'ERROR'}, f"Error processing XML file: {str(e)}")
            print(f"Exception: Error processing XML file: {str(e)}")
            import traceback
            traceback.print_exc()

        finally:
            print("XML processing completed.")

    def spawn_placement_objects(self, context, root):
        """Read every StadiumModel+TransformEntity pair matching a
        VA_FLAG_*/VA_TREE_A0_*/VA_TREE_B0_* dataList key and spawn a
        placement object at the correct transform, so imported stadiums can
        be edited (moved/rotated/re-textured) and re-exported. Coordinate
        conversion is the exact inverse of EXPORT_OT_flagarea_fpkd's write.
        """
        # Clear any previously-imported placement objects so re-importing
        # the same stadium doesn't duplicate them.
        for name_prefix in ("VA_FLAG_", "VA_TREE_A0", "VA_TREE_B0"):
            for obj in [o for o in context.scene.objects if o.name.startswith(name_prefix)]:
                bpy.data.objects.remove(obj, do_unlink=True)

        entities = root.findall(".//entity[@class='StadiumModel']")
        print(f"[LightManager] spawn_placement_objects: found {len(entities)} StadiumModel entities in XML")
        if not entities:
            self.report({'WARNING'}, "No StadiumModel entities found in this flagarea's XML — nothing to place. "
                                      "(The dataList/texture entries you see can exist without placed objects.)")
            return

        # Index TransformEntity blocks by addr for lookup
        transforms_by_addr = {}
        for te in root.findall(".//entity[@class='TransformEntity']"):
            transforms_by_addr[te.get('addr')] = te
        print(f"[LightManager] spawn_placement_objects: found {len(transforms_by_addr)} TransformEntity entities")

        _pes_get_objects_collection(context)
        spawned = 0
        skipped_reasons = []

        for entity in entities:
            name_el = entity.find(".//property[@name='name']/value")
            name = name_el.text if name_el is not None else None
            if not name:
                skipped_reasons.append("entity with no <name> property")
                continue

            if name.startswith("VA_TREE_A0"):
                variant_key, kind = 'TREE_A0', 'TREE'
            elif name.startswith("VA_TREE_B0"):
                variant_key, kind = 'TREE_B0', 'TREE'
            elif name.startswith("VA_FLAG_"):
                kind = 'FLAG'
                model_el = entity.find(".//property[@name='modelFile']/value")
                model_file = model_el.text if model_el is not None else ""
                if "standsFlagC" in model_file:
                    variant_key = 'FLAG_C'
                elif "standsFlagD" in model_file:
                    variant_key = 'FLAG_D'
                else:
                    variant_key = 'FLAG_A'
            else:
                continue  # not a flag/tree entity we manage (e.g. StadiumAnime) — not a skip worth reporting

            transform_el = entity.find(".//property[@name='transform']/value")
            transform_addr = transform_el.text if transform_el is not None else None
            te = transforms_by_addr.get(transform_addr) if transform_addr else None
            if te is None:
                msg = f"{name}: no matching TransformEntity (addr {transform_addr})"
                print(f"[LightManager] {msg} — skipping")
                skipped_reasons.append(msg)
                continue

            scale_val = te.find(".//property[@name='transform_scale']/value")
            rot_val = te.find(".//property[@name='transform_rotation_quat']/value")
            trans_val = te.find(".//property[@name='transform_translation']/value")
            if scale_val is None or rot_val is None or trans_val is None:
                msg = f"{name}: TransformEntity missing scale/rotation/translation property"
                print(f"[LightManager] {msg}")
                skipped_reasons.append(msg)
                continue

            raw_scale = (float(scale_val.get('x', 1)), float(scale_val.get('y', 1)), float(scale_val.get('z', 1)))
            raw_rot = (float(rot_val.get('x', 0)), float(rot_val.get('y', 0)), float(rot_val.get('z', 0)), float(rot_val.get('w', 1)))
            raw_trans = (float(trans_val.get('x', 0)), float(trans_val.get('y', 0)), float(trans_val.get('z', 0)))

            addon_dir = os.path.dirname(__file__)
            if kind == 'FLAG':
                obj_path = os.path.join(addon_dir, "resources-peslightmanager", "Objects", "va_flag_001", "flagmodel.obj")
            else:
                obj_path = os.path.join(addon_dir, "resources-peslightmanager", "Objects", TREE_VARIANTS[variant_key]['obj_file'])
            if not os.path.isfile(obj_path):
                msg = f"{name}: model file not found at {obj_path}"
                print(f"[LightManager] {msg}")
                skipped_reasons.append(msg)
                continue

            imported = _pes_import_obj(obj_path)
            if imported is None:
                msg = f"{name}: OBJ import failed (see console above for the importer's own error)"
                print(f"[LightManager] {msg}")
                skipped_reasons.append(msg)
                continue

            imported.name = name
            try:
                imported.rotation_mode = 'QUATERNION'
            except AttributeError:
                pass

            # Inverse of the export-side conversion (x/rotation_x/rotation_w
            # unchanged; y/z swapped with a sign flip).
            imported.location = (raw_trans[0], -raw_trans[2], raw_trans[1])
            imported.rotation_quaternion = (raw_rot[3], raw_rot[0], -raw_rot[2], raw_rot[1])  # (w, x, y, z)

            if kind == 'FLAG':
                scale_mult = FLAG_VARIANTS[variant_key]['export_scale_mult']
                imported.scale = tuple(v / scale_mult for v in raw_scale)
                imported["va_kind"] = variant_key
                imported["objectType"] = FLAG_VARIANTS[variant_key]['object_type']

                # Match the texture_id embedded in the modelFile name to an
                # already-loaded texture_map slot, so the flag shows its
                # real texture and re-exports correctly.
                tex_id_match = re.search(r'_(\d{4})\.fmdl$', model_file)
                tex_id = tex_id_match.group(1) if tex_id_match else "0000"
                imported["texture_id"] = tex_id
                tex_item = next((t for t in context.scene.pes_texture_map if t.tex_id == tex_id), None)
                tex_path = bpy.path.abspath(tex_item.image.filepath) if (tex_item and tex_item.image) else None
                _pes_setup_flag_material(imported, tex_path)
            else:
                imported.scale = raw_scale
                imported["va_kind"] = variant_key
                imported["objectType"] = variant_key
                addon_tex_dir = os.path.join(addon_dir, "resources-peslightmanager", "Objects", "va_tree")
                for slot in imported.material_slots:
                    if not slot.material:
                        continue
                    tex_file = None
                    for prefix, fname in TREE_VARIANTS[variant_key]['texture_map'].items():
                        if slot.material.name.startswith(prefix):
                            tex_file = fname
                            break
                    if not tex_file:
                        continue
                    material = slot.material
                    material.use_nodes = True
                    material.blend_method = 'CLIP'
                    nodes = material.node_tree.nodes
                    tnode = next((n for n in nodes if n.type == 'TEX_IMAGE'), None)
                    if tnode is None:
                        tnode = nodes.new(type='ShaderNodeTexImage')
                    tex_path = os.path.join(addon_tex_dir, tex_file)
                    if os.path.isfile(tex_path):
                        existing = next((img for img in bpy.data.images
                                          if bpy.path.abspath(img.filepath) == bpy.path.abspath(tex_path)), None)
                        tnode.image = existing if existing else bpy.data.images.load(tex_path)
                    shader = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
                    if shader is not None and tnode.image is not None:
                        material.node_tree.links.new(tnode.outputs["Color"], shader.inputs["Base Color"])
                        material.node_tree.links.new(tnode.outputs["Alpha"], shader.inputs["Alpha"])

            addr_el = entity.get('addr')
            imported["hex_value"] = addr_el if addr_el else "0x00000000"
            imported["hex_transform"] = transform_addr if transform_addr else "0x00000000"

            spawned += 1

        print(f"[LightManager] spawn_placement_objects: spawned {spawned}, skipped {len(skipped_reasons)}")
        for reason in skipped_reasons:
            print(f"[LightManager]   skipped — {reason}")

        if spawned:
            self.report({'INFO'}, f"Spawned {spawned} flag/tree placement object(s) in the viewport.")
        if skipped_reasons:
            preview = "; ".join(skipped_reasons[:3])
            more = f" (+{len(skipped_reasons) - 3} more, see console)" if len(skipped_reasons) > 3 else ""
            self.report({'WARNING'}, f"{len(skipped_reasons)} entit(y/ies) found but not placed: {preview}{more}")
        if not spawned and not skipped_reasons:
            self.report({'WARNING'}, f"Found {len(entities)} StadiumModel entities but none matched a "
                                      f"VA_FLAG_/VA_TREE_A0/VA_TREE_B0 name — check the console for their actual names.")

    def load_all_texture_slots(self, context, has_va_flag_or_tree):
        """Load all existing texture slots from the target directory"""
        try:
            # Get the target path where textures should be stored
            target_path = context.scene.pes_texture_target_path
            if not target_path or not os.path.exists(target_path):
                self.report({'ERROR'}, "Target texture path not found")
                print(f"Error: Target texture path not found or doesn't exist: {target_path}")
                return

            print(f"=== Loading all texture slots from: {target_path} ===")

            # Find all existing texture files with pattern XXXX_bsm.dds
            existing_textures = {}
            backup_files = set()
            
            for file in os.listdir(target_path):
                if file.endswith('.backup'):
                    backup_files.add(file)
                    continue
                    
                if re.match(r'\d{4}_bsm\.dds$', file):
                    tex_id = file[:4]
                    dds_path = os.path.join(target_path, file)
                    ftex_file = f"{tex_id}_bsm.ftex"
                    ftex_path = os.path.join(target_path, ftex_file)
                    
                    existing_textures[tex_id] = {
                        'dds_file': file,
                        'dds_path': dds_path,
                        'ftex_path': ftex_path,
                        'has_ftex': os.path.exists(ftex_path)
                    }
                    
                    print(f"Found texture slot: {tex_id} (DDS: ✓, FTEX: {'✓' if os.path.exists(ftex_path) else '✗'})")

            # If no textures found and no VA_FLAG/VA_TREE, load default
            if not existing_textures and not has_va_flag_or_tree:
                print("No existing textures found and no VA_FLAG/VA_TREE items. Loading default texture...")
                self.load_default_texture(context)
                return

            # Clear existing texture map
            context.scene.pes_texture_map.clear()
            print("Cleared existing texture map")

            # If no textures found but we have VA_FLAG/VA_TREE, just clear and exit
            if not existing_textures:
                print("No texture slots found, but VA_FLAG/VA_TREE items exist. Texture map cleared.")
                return

            # Sort texture IDs numerically
            sorted_tex_ids = sorted(existing_textures.keys(), key=lambda x: int(x))
            
            # Load each existing texture
            loaded_count = 0
            for tex_id in sorted_tex_ids:
                texture_info = existing_textures[tex_id]
                
                try:
                    print(f"Loading texture slot {tex_id}...")
                    
                    # Create texture map entry
                    item = context.scene.pes_texture_map.add()
                    item.tex_id = tex_id
                    item.file_name = texture_info['dds_file']
                    item.file_path = texture_info['dds_path']
                    item.modified = False
                    item.new = False  # These are existing textures

                    # Load the image into Blender
                    try:
                        # Remove existing image with same name if it exists
                        existing_image = bpy.data.images.get(texture_info['dds_file'])
                        if existing_image:
                            print(f"Removing existing image: {texture_info['dds_file']}")
                            bpy.data.images.remove(existing_image)
                        
                        # Load new image
                        print(f"Loading image from: {texture_info['dds_path']}")
                        image = bpy.data.images.load(texture_info['dds_path'])
                        image.use_fake_user = True
                        image.name = texture_info['dds_file']
                        
                        # Force preview generation
                        safe_preview_ensure(image)
                        bpy.context.view_layer.update()
                        
                        # Assign image to texture map item
                        item.image = image
                        
                        print(f"Successfully loaded texture {tex_id}: {image.size[0]}x{image.size[1]}")
                        
                        # Verify preview was created
                        if hasattr(image, 'preview') and image.preview and image.preview.icon_id:
                            print(f"Preview created for {tex_id}, icon_id: {image.preview.icon_id}")
                        else:
                            print(f"Warning: Preview not created for {tex_id}")
                            # Try again
                            safe_preview_ensure(image)
                        
                        loaded_count += 1
                        
                    except Exception as e:
                        print(f"Failed to load image for texture {tex_id}: {str(e)}")
                        item.image = None
                        # Don't fail the entire operation, just log the error
                        
                except Exception as e:
                    print(f"Failed to create texture map entry for {tex_id}: {str(e)}")
                    continue

            # Create FTEX files for any DDS files that don't have them
            ftextool_path = os.path.join(os.path.dirname(__file__), "resources-peslightmanager", "FtexTool", "FtexTool.exe")
            if os.path.exists(ftextool_path):
                for tex_id in sorted_tex_ids:
                    texture_info = existing_textures[tex_id]
                    if not texture_info['has_ftex']:
                        try:
                            print(f"Creating missing FTEX for {tex_id}...")
                            cmd = build_tool_command(ftextool_path, "-f", "0", texture_info['dds_path'])
                            result = subprocess.run(cmd, check=True, capture_output=True, encoding="utf-8", errors="replace")
                            print(f"Created FTEX for {tex_id}")
                        except subprocess.CalledProcessError as e:
                            print(f"Failed to create FTEX for {tex_id}: {e}")
            else:
                print(f"FtexTool not found at: {ftextool_path}")

            # Force redraw of all relevant UI areas
            for area in context.screen.areas:
                if area.type == 'PROPERTIES':
                    area.tag_redraw()

            # Set first texture as selected if any were loaded
            if loaded_count > 0:
                context.scene.pes_texture_map_index = 0

            print(f"=== Loaded {loaded_count} texture slots successfully ===")
            self.report({'INFO'}, f"Loaded {loaded_count} existing texture slots")

        except Exception as e:
            error_msg = f"Failed to load texture slots: {str(e)}"
            self.report({'ERROR'}, error_msg)
            print(f"ERROR: {error_msg}")
            import traceback
            print("Full traceback:")
            traceback.print_exc()

    def process_textures(self, context, target_path):
        if not os.path.exists(target_path):
            self.report({'ERROR'}, f"Target path does not exist: {target_path}")
            return []

        # Check for existing numbered textures
        dds_files = [f for f in os.listdir(target_path) if re.match(r'\d{4}_bsm\.dds', f)]

        ftextool_path = os.path.join(os.path.dirname(__file__), "resources-peslightmanager", "FtexTool", "FtexTool.exe")
        if not os.path.exists(ftextool_path):
            self.report({'ERROR'}, f"FtexTool.exe not found at {ftextool_path}")
            return []

        texture_map = []

        # If no numbered textures found, check for _bsm files and create 0000_bsm files
        if not dds_files:
            bsm_dds = os.path.join(target_path, "_bsm.dds")
            bsm_ftex = os.path.join(target_path, "_bsm.ftex")

            if os.path.exists(bsm_dds) and os.path.exists(bsm_ftex):
                new_dds = os.path.join(target_path, "0000_bsm.dds")
                new_ftex = os.path.join(target_path, "0000_bsm.ftex")

                shutil.copy2(bsm_dds, new_dds)
                shutil.copy2(bsm_ftex, new_ftex)

                self.report({'INFO'}, "Created 0000_bsm.dds and 0000_bsm.ftex from _bsm files")
                dds_files = ["0000_bsm.dds"]

        for dds_file in dds_files:
            dds_path = os.path.join(target_path, dds_file)
            ftex_file = dds_file.replace('.dds', '.ftex')
            ftex_path = os.path.join(target_path, ftex_file)

            # If FTEX doesn't exist, convert DDS to FTEX
            if not os.path.exists(ftex_path):
                cmd = build_tool_command(ftextool_path, "-f", "0", dds_path)
                print(f"Executing command: {' '.join(cmd)}")

                try:
                    result = subprocess.run(cmd, check=True, capture_output=True, encoding="utf-8", errors="replace")
                    print(f"FtexTool output: {result.stdout}")

                    if not os.path.exists(ftex_path):
                        self.report({'ERROR'}, f"Failed to convert {dds_file}: FTEX file not created")
                        print(f"FtexTool error output: {result.stderr}")
                        continue
                except subprocess.CalledProcessError as e:
                    self.report({'ERROR'}, f"Failed to convert {dds_file}")
                    print(f"FtexTool error output: {e.stderr}")
                    print(f"FtexTool command: {e.cmd}")
                    print(f"FtexTool return code: {e.returncode}")
                    continue

            tex_id = dds_file[:4]
            texture_map.append({
                'tex_id': tex_id,
                'file_name': dds_file,
                'file_path': dds_path,
                'modified': False,
                'new': tex_id == '0000' and len(dds_files) == 1
            })

            # Read DDS header to get width, height, and format
            with open(dds_path, 'rb') as dds:
                dds.seek(12)
                height, width = struct.unpack('II', dds.read(8))

                dds.seek(84)
                fourcc = dds.read(4).decode('ascii')

                format_map = {
                    'DXT1': 'DXT1',
                    'DXT3': 'DXT3',
                    'DXT5': 'DXT5',
                }
                compression_format = format_map.get(fourcc, 'Unknown')

            print(f"Processed {dds_file}: {width}x{height}, Format: {compression_format}")
            self.report({'INFO'}, f"Processed {dds_file}")

        if not texture_map:
            self.report({'WARNING'}, "No textures were processed")

        return texture_map


class EXPORT_OT_flagarea_fpkd(Operator):
    """Export placed VA_FLAG_*/VA_TREE_A0_*/VA_TREE_B0_* objects back into a
    stadium's flagarea_st0##.fpk/.fpkd. Ported from PES Lightmanager v0.2.0d
    (OBJECT_OT_ExportObjectsOperator), adapted to this codebase's structure.

    This pipeline shells out to GzsTool.exe / FoxTool.exe / FtexTool.exe
    (Windows-only) and does raw text-splicing into FoxTool's decompiled XML,
    exactly as the old tool did — kept faithful rather than "modernized"
    onto this file's ElementTree-based entity writer, since the decompiled
    XML's exact formatting isn't guaranteed to round-trip through ET and the
    string-splice approach here is the one that's actually shipped and
    worked before.
    """
    bl_idname = "pes_flags_trees.export_flagarea_fpkd"
    bl_label = "Export Flagarea FPKD"
    bl_options = {'REGISTER'}

    def execute(self, context):
        self.dataSet = "0x00000000"
        self.cornerflag_model_folder = None
        self.cornerflag_texture_folder = None
        self.texture_id_mapping = {}

        if not self._check_export_path(context):
            return {'CANCELLED'}

        if not self._extract_fpkd(context):
            return {'CANCELLED'}

        self._reorder_objects(context)
        self._search_and_assign_texture_ids(context)

        if not self._create_textures(context):
            return {'CANCELLED'}

        if not self._pack_fpk(context):
            return {'CANCELLED'}

        if not self._fox2_export(context):
            return {'CANCELLED'}

        self.report({'INFO'}, "Flagarea export completed.")
        return {'FINISHED'}

    # ------------------------------------------------------------------
    # Step 1: validate/seed the target .fpk
    # ------------------------------------------------------------------
    def _extraction_folder(self, file_path):
        """GzsTool's own convention for where it extracts an archive:
        "some/dir/name.ext" -> "some/dir/name_ext" (only the FILENAME's
        own period(s) become underscores; the directory portion is never
        touched). Must replace only the basename, not the whole path —
        replacing every "." in the full path breaks the moment any parent
        folder's name itself contains a period (e.g. "St. Johnstone FC"),
        silently diverging from the real folder GzsTool extracts into and
        reads from, which surfaces as a confusing "file not found" deep
        inside GzsTool rather than an obvious path bug.
        """
        directory, filename = os.path.split(file_path)
        return os.path.join(directory, filename.replace(".", "_"))

    def _run_tool(self, cmd, fail_message):
        """Run a bundled-tool subprocess, capturing its stdout/stderr so a
        failure's real cause (GzsTool/FoxTool/FtexTool's own .NET exception
        text) is surfaced directly in Blender's error report, instead of
        just the generic exit code. Returns True on success; on failure,
        reports fail_message plus the tool's own output and returns False.
        """
        try:
            result = subprocess.run(cmd, check=True, capture_output=True, encoding="utf-8", errors="replace")
            if result.stdout:
                print(result.stdout)
            if result.stderr:
                print(result.stderr)
            return True
        except subprocess.CalledProcessError as e:
            detail = (e.stderr or e.stdout or str(e)).strip()
            # Keep the report readable — full detail is always in the console.
            preview = detail[-800:] if len(detail) > 800 else detail
            print(f"[LightManager] {fail_message}\n{detail}")
            self.report({'ERROR'}, f"{fail_message}: {preview}")
            return False

    def _check_export_path(self, context):
        file_path = context.scene.pes_flagarea_export_path
        if not file_path:
            self.report({'ERROR'}, "Please select a flagarea_st0##.fpk file inside your stadium's standsFlag folder.")
            return False

        directory, file_name = os.path.split(file_path)

        valid_location = (os.path.sep + "standsFlag" + os.path.sep + "#Win" + os.path.sep) in file_path
        if not (valid_location and file_name.lower().endswith(".fpk")):
            self.report({'ERROR'}, "Please select a valid .fpk file inside a standsFlag\\#Win\\ folder.")
            return False

        gzs_tool_path = os.path.join(os.path.dirname(__file__), "resources-peslightmanager", "Gzs", "GzsTool.exe")
        if not os.path.exists(gzs_tool_path):
            self.report({'ERROR'}, f"GzsTool.exe not found at {gzs_tool_path}")
            return False

        if os.path.exists(file_path) and os.path.getsize(file_path) > 48:
            self.new_fpk_path = file_path
            self.new_fpkd_path = file_path + "d"
            self._gzs_command = build_tool_command(gzs_tool_path, file_path)
            self._gzs_command2 = build_tool_command(gzs_tool_path, file_path + "d")
            return True

        # Empty/placeholder .fpk — seed it from the bundled template
        match = re.search(r'st0(\d{2})', directory)
        if not match:
            self.report({'ERROR'}, "Unable to find stadium ID (st0##) in the selected path.")
            return False

        self.report({'INFO'}, "Selected .fpk is empty — replacing with a template.")
        stadium_id = match.group(1)
        addon_dir = os.path.dirname(__file__)
        template_fpk_path = os.path.join(addon_dir, "resources-peslightmanager", "Templates", "flagarea_.fpk")
        template_fpkd_path = os.path.join(addon_dir, "resources-peslightmanager", "Templates", "flagarea_.fpkd")

        if not (os.path.exists(template_fpk_path) and os.path.exists(template_fpkd_path)):
            self.report({'ERROR'}, "Bundled flagarea template (.fpk/.fpkd) not found in resources-peslightmanager/Templates.")
            return False

        for fname in os.listdir(directory):
            if fname.lower().endswith(('.fpk', '.fpkd')):
                try:
                    os.remove(os.path.join(directory, fname))
                except OSError:
                    pass

        shutil.copy(template_fpk_path, directory)
        shutil.copy(template_fpkd_path, directory)

        new_fpk_name = f"flagarea_st0{stadium_id}.fpk"
        new_fpkd_name = f"flagarea_st0{stadium_id}.fpkd"
        copied_fpk_path = os.path.join(directory, "flagarea_.fpk")
        copied_fpkd_path = os.path.join(directory, "flagarea_.fpkd")
        self.new_fpk_path = os.path.join(directory, new_fpk_name)
        self.new_fpkd_path = os.path.join(directory, new_fpkd_name)
        os.rename(copied_fpk_path, self.new_fpk_path)
        os.rename(copied_fpkd_path, self.new_fpkd_path)

        self._gzs_command = build_tool_command(gzs_tool_path, self.new_fpk_path)
        self._gzs_command2 = build_tool_command(gzs_tool_path, self.new_fpkd_path)
        context.scene.pes_flagarea_export_path = self.new_fpk_path
        return True

    # ------------------------------------------------------------------
    # Step 2: extract .fpk/.fpkd with GzsTool, then drop the bundled
    # flag/tree engine files and texture package into the extracted tree
    # ------------------------------------------------------------------
    def _extract_fpkd(self, context):
        if not self._run_tool(self._gzs_command, "GzsTool extraction (.fpk) failed"):
            return False
        if not self._run_tool(self._gzs_command2, "GzsTool extraction (.fpkd) failed"):
            return False
        try:
            self._insert_object_files(context)
        except RuntimeError as e:
            self.report({'ERROR'}, str(e))
            return False
        return True

    def _verified_copy(self, src, dst, retries=10, delay=0.2):
        """Copy a file and confirm it's actually durable/visible on disk
        afterward before moving on — a file just written by this (Python)
        process isn't necessarily instantly visible to a completely
        separate process (GzsTool.exe via mono) moments later, especially
        under iCloud Drive's "Desktop & Documents" sync, which can evict
        files to cloud-only placeholders. Polls briefly rather than
        assuming shutil.copy2 returning means the OS-level write settled.
        Raises RuntimeError with a clear message if it never stabilizes.
        """
        shutil.copy2(src, dst)
        expected_size = os.path.getsize(src)
        for _ in range(retries):
            if os.path.exists(dst) and os.path.getsize(dst) == expected_size:
                return
            time.sleep(delay)
        raise RuntimeError(
            f"Copied {os.path.basename(src)} to {dst}, but it isn't reliably visible on disk "
            f"afterward (still missing or wrong size after {retries * delay:.1f}s). If this project "
            f"folder is under iCloud Drive (Documents/Desktop sync), that's the likely cause — "
            f"try moving the project to a non-synced folder, or disable iCloud Desktop & Documents "
            f"sync, then retry."
        )

    def _insert_object_files(self, context):
        addon_dir = os.path.dirname(__file__)
        fpk_extracted_root = self._extraction_folder(self.new_fpk_path)

        # .../<fpk>_/Assets/pes16/model/bg/common/cornerflag
        common_model_root = os.path.join(fpk_extracted_root, "Assets", "pes16", "model", "bg", "common")
        os.makedirs(common_model_root, exist_ok=True)
        cornerflag_folder = os.path.join(common_model_root, "cornerflag")
        os.makedirs(cornerflag_folder, exist_ok=True)
        for f in os.listdir(cornerflag_folder):
            try:
                os.remove(os.path.join(cornerflag_folder, f))
            except OSError:
                pass

        flag_files = [
            "dml_mobH_audi_flagbearer_01_mob_prop_teamflag_home01.gani",
            "mob_prop_teamflag_anim_skel.ask",
            "mob_prop_teamflag_home01.fmdl",
            "mob_prop_teamflag_home01.skl",
            "mob_prop_teamflag_render_skel.ask",
            "mob_prop_teamflag_skel.frig",
            "standsFlagA_0000.fmdl",
            "standsFlagC_0000.fmdl",
            "standsFlagD_0000.fmdl",
        ]
        for fname in flag_files:
            src = os.path.join(addon_dir, "resources-peslightmanager", "Objects", "va_flag_001", fname)
            if os.path.exists(src):
                self._verified_copy(src, os.path.join(cornerflag_folder, fname))

        tree_files = ["va_tree001_a0.fmdl", "va_tree001_b0.fmdl"]
        for fname in tree_files:
            src = os.path.join(addon_dir, "resources-peslightmanager", "Objects", "va_tree", fname)
            if os.path.exists(src):
                self._verified_copy(src, os.path.join(cornerflag_folder, fname))

        self.cornerflag_model_folder = cornerflag_folder

        # .../<fpk>_/Assets/pes16/model/bg/common/standsFlag/sourceimages/tga/#windx11
        texture_folder = os.path.join(common_model_root, "standsFlag", "sourceimages", "tga", "#windx11")
        os.makedirs(texture_folder, exist_ok=True)

        texture_pkg_src = os.path.join(addon_dir, "resources-peslightmanager", "Objects", "va_flag_001", "textures")
        if os.path.isdir(texture_pkg_src):
            for item in os.listdir(texture_pkg_src):
                s = os.path.join(texture_pkg_src, item)
                d = os.path.join(texture_folder, item)
                if os.path.isfile(s):
                    self._verified_copy(s, d)
                elif os.path.isdir(s):
                    shutil.copytree(s, d, dirs_exist_ok=True)

        self.cornerflag_texture_folder = texture_folder

    # ------------------------------------------------------------------
    # Step 3: renumber every placed object and assign its final addr/transform
    # ------------------------------------------------------------------
    def _reorder_objects(self, context):
        def renumber(prefix, addr_base, transform_base):
            objs = [o for o in context.scene.objects if o.name.startswith(prefix)]
            for i, obj in enumerate(objs):
                obj.name = f"{prefix}_{i:04d}"
                addr = addr_base + i
                transform = transform_base + i
                # Matches the old tool's scheme exactly: "0x00" + decimal
                # digits (not a true hex conversion of the integer) — kept
                # for on-disk/address-range compatibility.
                obj["hex_value"] = f"0x00{addr:06d}"
                obj["hex_transform"] = f"0x00{transform:06d}"

        renumber("VA_FLAG", 300000, 310000)
        renumber("VA_TREE_A0", 400000, 410000)
        renumber("VA_TREE_B0", 500000, 510000)

    # ------------------------------------------------------------------
    # Step 4: dedupe flag textures to sequential ids
    # ------------------------------------------------------------------
    def _search_and_assign_texture_ids(self, context):
        flag_objects = [o for o in context.scene.objects if o.name.startswith("VA_FLAG_")]
        self.texture_id_mapping = {}

        for obj in flag_objects:
            texture_path = None
            for slot in obj.material_slots:
                if slot.material and slot.material.name.startswith("flag_material"):
                    if not slot.material.use_nodes:
                        continue
                    for node in slot.material.node_tree.nodes:
                        if node.type == 'TEX_IMAGE' and node.image:
                            texture_path = bpy.path.abspath(node.image.filepath)
                            break
                if texture_path:
                    break

            if not texture_path:
                obj["texture_id"] = "0000"
                continue

            if texture_path not in self.texture_id_mapping:
                self.texture_id_mapping[texture_path] = f"{len(self.texture_id_mapping):04d}"
            obj["texture_id"] = self.texture_id_mapping[texture_path]

    # ------------------------------------------------------------------
    # Step 5: resize/pad each distinct texture, duplicate per-id fmdl
    # variants and patch their embedded texture filename reference
    # ------------------------------------------------------------------
    def _resize_dds_top_anchor(self, input_path, output_path, x_factor, y_factor, allowed_sizes):
        if not PIL_AVAILABLE:
            raise RuntimeError("Pillow (PIL) is required to process custom flag textures but is not installed.")
        img = Image.open(input_path)
        width, height = img.size
        new_width = int(width * x_factor)
        new_height = int(height * y_factor)

        distorted = Image.new("RGBA", (new_width, new_height), (255, 255, 255, 255))
        paste_x = (new_width - width) // 2
        distorted.paste(img, (paste_x, 0))

        max_dimension = max(width, height)
        candidates = [s for s in allowed_sizes if s <= max_dimension] or [min(allowed_sizes)]
        new_size = max(candidates)
        final_size = min(allowed_sizes, key=lambda x: abs(x - new_size))

        resample = getattr(Image, "LANCZOS", None) or getattr(Image.Resampling, "LANCZOS", None)
        final_img = distorted.resize((final_size, final_size), resample)
        final_img.save(output_path)

    def _create_textures(self, context):
        if not self.texture_id_mapping:
            return True  # no custom flag textures in use — nothing to bake

        if not self.cornerflag_texture_folder or not self.cornerflag_model_folder:
            self.report({'ERROR'}, "Internal error: cornerflag folders not resolved.")
            return False

        allowed_sizes = [4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096]

        for blender_texture, tex_id in self.texture_id_mapping.items():
            if not os.path.isfile(blender_texture):
                self.report({'WARNING'}, f"Texture file not found, skipping: {blender_texture}")
                continue
            target_dds = os.path.join(self.cornerflag_texture_folder, f"{tex_id}_bsm.dds")
            try:
                self._resize_dds_top_anchor(blender_texture, target_dds, 1, 1.4545, allowed_sizes)
            except Exception as e:
                self.report({'ERROR'}, f"Failed to process texture {blender_texture}: {e}")
                return False

        # Duplicate/patch the standsFlagA/C/D_0000.fmdl base files once per
        # distinct texture id (cascading copy-then-patch, matching the old
        # tool: each iteration patches the file created by the previous one).
        num_textures = len(self.texture_id_mapping)
        pattern = re.compile(rb'(\d{4})_bsm\.tga')
        for tex_id in range(num_textures):
            for prefix in ("standsFlagA", "standsFlagC", "standsFlagD"):
                source_path = os.path.join(self.cornerflag_model_folder, f"{prefix}_{tex_id:04d}.fmdl")
                target_path = os.path.join(self.cornerflag_model_folder, f"{prefix}_{tex_id + 1:04d}.fmdl")
                if not os.path.exists(source_path):
                    continue
                with open(source_path, 'rb') as f:
                    content = f.read()
                match = pattern.search(content)
                if match:
                    start, end = match.span(1)
                    content = content[:start] + f"{tex_id:04d}".encode('utf-8') + content[end:]
                    with open(source_path, 'wb') as f:
                        f.write(content)
                if not os.path.exists(target_path):
                    shutil.copy2(source_path, target_path)

        return True

    # ------------------------------------------------------------------
    # Step 6: repack the .fpk with a freshly-generated manifest (it now
    # contains files that weren't in the original archive)
    # ------------------------------------------------------------------
    def _pack_fpk(self, context):
        pack_xml_path = f"{self.new_fpk_path}.xml"
        fpk_extracted_root = self._extraction_folder(self.new_fpk_path)
        archive_name = os.path.basename(self.new_fpk_path)

        # macOS (Finder, or copying to/from non-native volumes) litters
        # folders with metadata files that were never part of the archive —
        # .DS_Store, AppleDouble "._foo" resource-fork shadows, __MACOSX
        # folders. GzsTool has no idea what to do with these and dies on
        # the first one it can't read back. Filter them out of the manifest.
        def is_macos_junk(name):
            return name == ".DS_Store" or name.startswith("._") or name == ".localized"

        entries = []
        for root_dir, dirs, files in os.walk(fpk_extracted_root):
            dirs[:] = [d for d in dirs if d != "__MACOSX"]
            for fname in files:
                if is_macos_junk(fname):
                    continue
                rel = os.path.relpath(os.path.join(root_dir, fname), fpk_extracted_root).replace("\\", "/")
                entries.append(f'<Entry FilePath="/{rel}" />')

        xml_content = (
            '<?xml version="1.0"?>\n'
            '<ArchiveFile xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            f'xmlns:xsd="http://www.w3.org/2001/XMLSchema" xsi:type="FpkFile" Name="{archive_name}" FpkType="Fpk">\n'
            '  <Entries>\n    ' + '\n    '.join(entries) + '\n'
            '  </Entries>\n'
            '  <References />\n'
            '</ArchiveFile>\n'
        )
        with open(pack_xml_path, 'w') as f:
            f.write(xml_content)

        gzs_tool_path = os.path.join(os.path.dirname(__file__), "resources-peslightmanager", "Gzs", "GzsTool.exe")
        if not os.path.exists(gzs_tool_path):
            self.report({'ERROR'}, "GzsTool.exe not found")
            return False
        return self._run_tool(build_tool_command(gzs_tool_path, pack_xml_path), "Packing .fpk failed")

    # ------------------------------------------------------------------
    # Step 7: locate the flagarea .fox2 inside the extracted .fpkd tree,
    # decompile it, edit it, recompile it, and repack the .fpkd
    # ------------------------------------------------------------------
    def _fox2_export(self, context):
        fpkd_extracted_root = self._extraction_folder(self.new_fpkd_path)

        fox2_dir = None
        for i in range(100):
            candidate = os.path.join(fpkd_extracted_root, "Assets", "pes16", "model", "bg", f"st0{i:02d}", "standsFlag")
            if os.path.isdir(candidate):
                fox2_dir = candidate
                break
        if fox2_dir is None:
            self.report({'ERROR'}, "Could not locate the stadium's standsFlag folder inside the extracted .fpkd.")
            return False

        fox2_files = [f for f in os.listdir(fox2_dir) if f.endswith(".fox2")]
        if len(fox2_files) != 1:
            self.report({'ERROR'}, f"Expected exactly one .fox2 file in {fox2_dir}, found {len(fox2_files)}.")
            return False

        fox2_path = os.path.join(fox2_dir, fox2_files[0])
        foxtool_path = os.path.join(os.path.dirname(__file__), "resources-peslightmanager", "FoxTool", "FoxTool.exe")
        if not os.path.exists(foxtool_path):
            self.report({'ERROR'}, "FoxTool.exe not found")
            return False

        if not self._run_tool(build_tool_command(foxtool_path, fox2_path), "FoxTool decompile failed"):
            return False

        xml_path = fox2_path + ".xml"
        if not os.path.exists(xml_path):
            self.report({'ERROR'}, f"Decompiled XML not found: {xml_path}")
            return False

        self._backup_xml(xml_path)
        self._remove_stale_flag_tree_entries(xml_path, context)
        self._insert_class_declarations(xml_path)
        self._insert_datalist_entries(xml_path)
        self._get_dataset_value(xml_path)
        self._insert_stadiumanime_entity(xml_path, context)
        self._insert_placement_entities(xml_path, context, "VA_FLAG_", 'FLAG')
        self._insert_placement_entities(xml_path, context, "VA_TREE_A0", 'TREE_A0')
        self._insert_placement_entities(xml_path, context, "VA_TREE_B0", 'TREE_B0')

        # Sanity-check the edited XML is still well-formed before handing it
        # to FoxTool — a malformed splice here could make FoxTool silently
        # emit a truncated/empty .fox2 rather than erroring.
        try:
            ET.parse(xml_path)
        except ET.ParseError as e:
            self.report({'ERROR'}, f"The edited XML is not well-formed XML before recompiling — "
                                    f"aborting rather than risk a corrupt .fox2: {e}")
            return False

        fox2_size_before = os.path.getsize(fox2_path) if os.path.exists(fox2_path) else 0

        if not self._run_tool(build_tool_command(foxtool_path, xml_path), "FoxTool recompile failed"):
            return False

        # FoxTool has been observed to exit 0 while still writing a
        # truncated/empty .fox2 under some conditions — verify the output
        # actually looks like a real compiled file before packing it,
        # rather than silently shipping a corrupt archive.
        fox2_size_after = os.path.getsize(fox2_path) if os.path.exists(fox2_path) else 0
        MIN_PLAUSIBLE_FOX2_SIZE = 64  # a real fox2 has a header + at least the DataSet entity
        if fox2_size_after < MIN_PLAUSIBLE_FOX2_SIZE:
            self.report({'ERROR'}, f"FoxTool recompile produced a suspiciously small/empty .fox2 "
                                    f"({fox2_size_after} bytes, was {fox2_size_before} bytes before). "
                                    f"Aborting before packing a corrupt file. Check the console output "
                                    f"above this point for anything FoxTool printed.")
            return False
        print(f"[LightManager] Recompiled fox2: {fox2_size_before} -> {fox2_size_after} bytes")

        # Repack the .fpkd from its (unmodified-manifest) extracted tree —
        # the .fox2 was recompiled in place, no new files were added to it.
        fpkd_manifest = None
        for fname in os.listdir(os.path.dirname(fpkd_extracted_root)):
            if fname.startswith("flagarea_") and fname.endswith(".fpkd.xml"):
                fpkd_manifest = os.path.join(os.path.dirname(fpkd_extracted_root), fname)
                break
        if not fpkd_manifest:
            self.report({'ERROR'}, "Could not find the .fpkd manifest XML to repack.")
            return False

        gzs_tool_path = os.path.join(os.path.dirname(__file__), "resources-peslightmanager", "Gzs", "GzsTool.exe")
        if not self._run_tool(build_tool_command(gzs_tool_path, fpkd_manifest), "Repacking .fpkd failed"):
            return False

        self._convert_dds_to_ftex()
        return True

    def _backup_xml(self, xml_path):
        backup_dir = os.path.join(os.path.dirname(xml_path), "backup")
        os.makedirs(backup_dir, exist_ok=True)
        backup_path = os.path.join(backup_dir, f"{os.path.basename(xml_path)}_backup.xml")
        try:
            shutil.copy2(xml_path, backup_path)
        except OSError as e:
            print(f"[LightManager] Could not back up {xml_path}: {e}")

    def _remove_stale_flag_tree_entries(self, xml_path, context):
        """Strip every dataList value-row, StadiumAnime entity, and
        StadiumModel/TransformEntity pair this tool manages (VA_FLAG_*,
        VA_TREE_A0_*, VA_TREE_B0_*, StadiumAnime_Flags) from the freshly
        decompiled XML before inserting a fresh set.

        Without this, re-exporting an already-populated stadium (e.g. one
        you just imported) appends a second, duplicate set of entries
        alongside the originals — including duplicate entity addresses,
        since renumbering always starts back at the same fixed offsets.
        FoxTool's compiler tolerates this just enough to emit a non-empty
        but structurally broken .fox2, which then fails to decompile again
        on the next import ("EndOfStreamException" / "no element found").
        Anything NOT managed by this tool (other dataList keys, other
        entity classes) is left untouched.
        """
        with open(xml_path, 'r', encoding='utf-8') as f:
            content = f.read()

        managed_prefixes = ("VA_FLAG_", "VA_TREE_A0", "VA_TREE_B0")

        # 1) dataList value rows for our keys (plus the StadiumAnime_Flags
        #    dataList key, which is only ever added alongside flags).
        def is_managed_key(key):
            return key == "StadiumAnime_Flags" or key.startswith(managed_prefixes)

        value_row = re.compile(r'[ \t]*<value key="([^"]+)">[^<]*</value>\n?')
        removed_keys = 0
        def strip_value_row(m):
            nonlocal removed_keys
            if is_managed_key(m.group(1)):
                removed_keys += 1
                return ""
            return m.group(0)
        content = value_row.sub(strip_value_row, content)

        # 2) Any existing StadiumAnime entity (there should be at most one,
        #    but strip all to be safe against a previous partial/duplicate
        #    export).
        stadiumanime_entity = re.compile(r'[ \t]*<entity class="StadiumAnime".*?</entity>\n?', re.DOTALL)
        content, removed_anime = stadiumanime_entity.subn("", content)

        # 3) StadiumModel entities whose <name> matches our prefixes, plus
        #    their paired TransformEntity (matched via the StadiumModel's
        #    own "transform" property addr). Two passes: find+collect
        #    first, then remove, so removing text doesn't shift offsets
        #    out from under a still-pending match.
        entity_block = re.compile(r'[ \t]*<entity class="(StadiumModel|TransformEntity)"[^>]*addr="([^"]*)"[^>]*>.*?</entity>\n?', re.DOTALL)
        name_prop = re.compile(r'<property name="name"[^>]*>\s*<value>([^<]*)</value>')
        transform_prop = re.compile(r'<property name="transform"[^>]*>\s*<value>([^<]*)</value>')

        transform_addrs_to_remove = set()
        blocks_to_remove = []
        for m in entity_block.finditer(content):
            cls, addr, block_text = m.group(1), m.group(2), m.group(0)
            if cls != 'StadiumModel':
                continue
            name_m = name_prop.search(block_text)
            if not name_m or not name_m.group(1).startswith(managed_prefixes):
                continue
            blocks_to_remove.append(m.span())
            transform_m = transform_prop.search(block_text)
            if transform_m:
                transform_addrs_to_remove.add(transform_m.group(1))

        # Second pass: also remove the paired TransformEntity blocks by addr.
        for m in entity_block.finditer(content):
            cls, addr = m.group(1), m.group(2)
            if cls == 'TransformEntity' and addr in transform_addrs_to_remove:
                blocks_to_remove.append(m.span())

        # Remove collected spans back-to-front so earlier spans stay valid.
        for start, end in sorted(blocks_to_remove, reverse=True):
            content = content[:start] + content[end:]

        with open(xml_path, 'w', encoding='utf-8') as f:
            f.write(content)

        print(f"[LightManager] Pre-export cleanup: removed {removed_keys} stale dataList row(s), "
              f"{removed_anime} StadiumAnime entity, {len(blocks_to_remove)} StadiumModel/TransformEntity block(s)")

    def _insert_class_declarations(self, xml_path):
        with open(xml_path, 'r', encoding='utf-8') as f:
            content = f.read()
        if "</classes>" not in content:
            return
        needed = [
            '  <class name="StadiumAnime" super="" version="3" />',
            '    <class name="StadiumModel" super="" version="3" />',
            '    <class name="TransformEntity" super="" version="0" />',
        ]
        # Only add class defs FoxTool didn't already emit (idempotent re-export)
        missing = [line for line in needed if line.strip().split('"')[1] not in content]
        if not missing:
            return
        content = content.replace("</classes>", "\n".join(missing) + "\n  </classes>")
        with open(xml_path, 'w', encoding='utf-8') as f:
            f.write(content)

    def _insert_datalist_entries(self, xml_path):
        with open(xml_path, 'r') as f:
            content = f.read()
        idx = content.find('<property name="dataList"')
        if idx == -1:
            return
        # Find the </property> that closes THIS dataList property block and
        # insert right before it, rather than assuming a fixed number of
        # pre-existing <value> lines (that assumption breaks if a prior
        # cleanup pass ever leaves the block empty).
        idx_insert = content.find('</property>', idx)
        if idx_insert == -1:
            return
        # Back up to the start of that line so we insert with matching
        # indentation rather than mid-line.
        line_start = content.rfind('\n', 0, idx_insert) + 1
        idx_insert = line_start

        objs = [o for o in bpy.context.scene.objects
                if o.name.startswith("VA_FLAG_") or o.name.startswith("VA_TREE_A0") or o.name.startswith("VA_TREE_B0")]
        lines = [f'          <value key="{o.name}">{o["hex_value"]}</value>' for o in objs]
        if objs:
            lines.append('          <value key="StadiumAnime_Flags">0x00001488</value>')

        content = content[:idx_insert] + '\n'.join(lines) + ("\n" if lines else "") + content[idx_insert:]
        with open(xml_path, 'w') as f:
            f.write(content)

    def _get_dataset_value(self, xml_path):
        with open(xml_path, 'r') as f:
            content = f.read()
        idx = content.find('addr="')
        if idx != -1:
            start = idx + len('addr="')
            self.dataSet = content[start:start + 10]

    def _insert_stadiumanime_entity(self, xml_path, context):
        flag_objects = [o for o in context.scene.objects if o.name.startswith("VA_FLAG_")]
        if not flag_objects:
            return  # no flags placed — nothing for the animation driver to reference

        with open(xml_path, 'r') as f:
            content = f.read()
        idx_entities = content.find('</entities>')
        if idx_entities == -1:
            return
        idx_insert = idx_entities - 1

        lines = [
            '   <entity class="StadiumAnime" classVersion="3" addr="0x00001488" unknown1="280" unknown2="0">',
            '      <staticProperties>',
            '        <property name="name" type="String" container="StaticArray" arraySize="1">',
            '          <value>StadiumAnime_Flags</value>',
            '        </property>',
            '        <property name="dataSet" type="EntityHandle" container="StaticArray" arraySize="1">',
            f'          <value>{self.dataSet}</value>',
            '        </property>',
            '        <property name="animationGroup" type="int32" container="StaticArray" arraySize="1">',
            '          <value>10</value>',
            '        </property>',
            '        <property name="animeFiles" type="FilePtr" container="DynamicArray" arraySize="1">',
            '          <value>/Assets/pes16/model/bg/common/cornerflag/dml_mobH_audi_flagbearer_01_mob_prop_teamflag_home01.gani</value>',
            '        </property>',
            '        <property name="anmSklFile" type="FilePtr" container="StaticArray" arraySize="1">',
            '          <value>/Assets/pes16/model/bg/common/cornerflag/mob_prop_teamflag_anim_skel.ask</value>',
            '        </property>',
            '        <property name="renderSklFile" type="FilePtr" container="StaticArray" arraySize="1">',
            '          <value>/Assets/pes16/model/bg/common/cornerflag/mob_prop_teamflag_render_skel.ask</value>',
            '        </property>',
            '        <property name="rigFile" type="FilePtr" container="StaticArray" arraySize="1">',
            '          <value>/Assets/pes16/model/bg/common/cornerflag/mob_prop_teamflag_skel.frig</value>',
            '        </property>',
            '        <property name="sklFile" type="FilePtr" container="StaticArray" arraySize="1">',
            '          <value>/Assets/pes16/model/bg/common/cornerflag/mob_prop_teamflag_home01.skl</value>',
            '        </property>',
            f'        <property name="models" type="EntityPtr" container="DynamicArray" arraySize="{len(flag_objects)}">',
        ]
        for obj in flag_objects:
            lines.append(f'          <value>{obj["hex_value"]}</value>')
        lines += [
            '        </property>',
            '      </staticProperties>',
            '      <dynamicProperties />',
            '    </entity>',
        ]

        content = content[:idx_insert] + '\n'.join(lines) + "\n" + content[idx_insert:]
        with open(xml_path, 'w') as f:
            f.write(content)

    def _insert_placement_entities(self, xml_path, context, name_prefix, kind):
        objs = [o for o in context.scene.objects if o.name.startswith(name_prefix)]
        if not objs:
            return

        with open(xml_path, 'r') as f:
            content = f.read()
        idx_entities = content.find('</entities>')
        if idx_entities == -1:
            return
        idx_insert = idx_entities - 1

        blocks = []
        for obj in objs:
            model_file, scale_mult = self._resolve_model_file(obj, kind)
            blocks.append(self._stadiummodel_block(obj, model_file, scale_mult))

        content = content[:idx_insert] + "\n".join(blocks) + "\n" + content[idx_insert:]
        with open(xml_path, 'w') as f:
            f.write(content)

    def _resolve_model_file(self, obj, kind):
        if kind == 'FLAG':
            variant_key = obj.get("va_kind", "FLAG_A")
            info = FLAG_VARIANTS.get(variant_key, FLAG_VARIANTS['FLAG_A'])
            tex_id = obj.get("texture_id", "0000")
            model_file = f"/Assets/pes16/model/bg/common/cornerflag/{info['model_prefix']}_{tex_id}.fmdl"
            return model_file, info['export_scale_mult']
        elif kind == 'TREE_A0':
            return TREE_VARIANTS['TREE_A0']['model_file'], 1.0
        else:
            return TREE_VARIANTS['TREE_B0']['model_file'], 1.0

    def _stadiummodel_block(self, obj, model_file, scale_mult):
        q = obj.rotation_quaternion
        loc = obj.location
        scale = obj.scale

        format_dict = {
            "addr": obj["hex_value"],
            "transform_addr": obj["hex_transform"],
            "name": obj.name,
            "dataset_addr": self.dataSet if self.dataSet.startswith("0x") else f"0x{self.dataSet}",
            "model_file": model_file,
            "va_flags": "7",
            "scale_x": round(scale.x * scale_mult, 2),
            "scale_y": round(scale.y * scale_mult, 2),
            "scale_z": round(scale.z * scale_mult, 2),
            "rotation_x": round(q.x, 4),
            "rotation_y": round(q.z, 4),
            "rotation_z": round(-q.y, 4),
            "rotation_w": round(q.w, 4),
            "translation_x": round(loc.x, 4),
            "translation_y": round(loc.z, 4),
            "translation_z": round(-loc.y, 4),
        }
        return STADIUMMODEL_TEMPLATE.format(**format_dict)

    def _convert_dds_to_ftex(self):
        if not self.cornerflag_texture_folder:
            return
        ftex_tool_path = os.path.join(os.path.dirname(__file__), "resources-peslightmanager", "FtexTool", "FtexTool.exe")
        if not os.path.exists(ftex_tool_path):
            print("[LightManager] FtexTool.exe not found — skipping .ftex conversion.")
            return
        for fname in os.listdir(self.cornerflag_texture_folder):
            if fname.lower().endswith("_bsm.dds"):
                fpath = os.path.join(self.cornerflag_texture_folder, fname)
                try:
                    subprocess.run(build_tool_command(ftex_tool_path, "-f", "0", fpath),
                                    check=True, capture_output=True, encoding="utf-8", errors="replace")
                except subprocess.CalledProcessError as e:
                    detail = (e.stderr or e.stdout or str(e)).strip()
                    print(f"[LightManager] FtexTool failed on {fpath}: {detail}")




class PES_PT_flags_trees_panel(Panel):
    """Standalone Flags & Trees panel — same controls as the Flags tab in
    PES Lightmanager 3.2, just without needing that addon installed."""
    bl_label = "PES Flags & Trees"
    bl_idname = "pes_flags_trees_panel"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "scene"

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        # Import Flagarea FPKD button
        layout.operator("pes_flags_trees.import_flagarea_fpkd", text="Import Flagarea FPKD", icon='IMPORT')

        # Add Dynamic Objects (flags & trees)
        box = layout.box()
        box.label(text="Add Dynamic Objects", icon='ADD')
        row = box.row(align=True)
        op = row.operator("pes_flags_trees.add_flag", text="+ Flag (Large)")
        op.variant = 'FLAG_C'
        op = row.operator("pes_flags_trees.add_flag", text="+ Flag (Medium)")
        op.variant = 'FLAG_A'
        op = row.operator("pes_flags_trees.add_flag", text="+ Flag (Small)")
        op.variant = 'FLAG_D'
        row = box.row(align=True)
        op = row.operator("pes_flags_trees.add_tree", text="+ Tree (a0)")
        op.variant = 'TREE_A0'
        op = row.operator("pes_flags_trees.add_tree", text="+ Tree (b0)")
        op.variant = 'TREE_B0'
        box.operator("pes_flags_trees.apply_flag_texture", text="Apply Texture to Selected Flag(s)", icon='TEXTURE')

        # Export Dynamic Objects
        exp_box = layout.box()
        exp_box.label(text="Export Dynamic Objects", icon='EXPORT')
        exp_box.prop(scene, "pes_flagarea_export_path", text="flagarea_st0##.fpk")
        exp_box.operator("pes_flags_trees.export_flagarea_fpkd", text="Export to FLAGAREA", icon='EXPORT')

        # Checkbox to toggle Texture Map visibility
        layout.prop(scene, "show_texture_map", text="Show Texture Map")

        # Improved Texture Map Display
        if scene.show_texture_map and len(scene.pes_texture_map) > 0:
            box = layout.box()
            
            # Header with title and controls
            header = box.row()
            header.label(text="Texture Map", icon='TEXTURE')
            
            # Add New Slot button
            header.operator("pes_flags_trees.add_new_texture_slot", text="Add New Slot", icon='ADD')

            # Display mode: List with perfect square previews
            for i, texture in enumerate(scene.pes_texture_map):
                # Create a prominent box for each texture
                texture_box = box.box()
                
                # Main content row
                main_row = texture_box.row(align=True)
                
                # Left side: Perfect 1:1 square preview
                # Use a fixed-width column for the square preview
                preview_col = main_row.column(align=True)
                preview_col.ui_units_x = 3  # Fixed width for square aspect ratio
                
                # Create square preview container
                square_container = preview_col.column(align=True)
                
                # Large clickable preview - make it square
                if texture.image and hasattr(texture.image, 'preview') and texture.image.preview:
                    # Create a square button
                    preview_row = square_container.row()
                    preview_row.scale_y = 3.0  # Height to match width
                    preview_row.scale_x = 1.0  # Keep width normal for square
                    
                    # Square preview button (no text, just icon)
                    select_op = preview_row.operator("pes_flags_trees.select_texture", 
                                                    text="", 
                                                    icon_value=texture.image.preview.icon_id, 
                                                    emboss=False)  # Remove border for cleaner look
                    select_op.index = i
                else:
                    # Fallback for missing preview - also square
                    preview_row = square_container.row()
                    preview_row.scale_y = 3.0
                    preview_row.scale_x = 1.0
                    
                    select_op = preview_row.operator("pes_flags_trees.select_texture", 
                                                    text="", 
                                                    icon='TEXTURE', 
                                                    emboss=False)
                    select_op.index = i
                
                # Right side: Texture information and controls
                info_col = main_row.column(align=True)
                
                # Top row: ID button (always same size, red when selected)
                id_row = info_col.row()
                id_row.scale_y = 1.8  # Make it prominent
                
                # ID selection button - always same size, red when selected
                id_button = id_row.row()
                if i == scene.pes_texture_map_index:
                    id_button.alert = True  # Makes it red when selected
                
                select_id_op = id_button.operator("pes_flags_trees.select_texture", 
                                                text=f"ID: {texture.tex_id}")
                select_id_op.index = i
                
                # File information row
                file_row = info_col.row()
                file_row.scale_y = 0.8  # Smaller text
                file_row.label(text=f"File: {texture.file_name}")
                
                # Image dimensions and status row
                details_row = info_col.row()
                details_row.scale_y = 0.8  # Smaller text
                
                # Left side: dimensions
                dim_split = details_row.split(factor=0.6)
                if texture.image:
                    dim_split.label(text=f"Size: {texture.image.size[0]}x{texture.image.size[1]}")
                else:
                    dim_split.label(text="Size: No image")
                
                # Right side: status
                status_col = dim_split.column()
                status_row = status_col.row()
                status_row.alignment = 'RIGHT'
                status_row.scale_y = 0.8
                
                if texture.modified:
                    status_row.label(text="Modified", icon='RADIOBUT_ON')
                elif texture.new:
                    status_row.label(text="New", icon='ADD')
                else:
                    status_row.label(text="Original", icon='RADIOBUT_OFF')
                
                # Action buttons row - only show for selected texture
                if i == scene.pes_texture_map_index:
                    action_row = info_col.row(align=True)
                    action_row.scale_y = 1.2  # Make buttons more prominent
                    
                    # Change File button with better styling
                    change_btn = action_row.row()
                    change_btn.operator("pes_flags_trees.edit_texture", 
                                      text="Change File", 
                                      icon='FILEBROWSER')
                    
                    # Remove button with better styling (only if not default texture)
                    if texture.tex_id != "0000":
                        remove_btn = action_row.row()
                        remove_btn.alert = True  # Make remove button red
                        remove_btn.operator("pes_flags_trees.remove_texture", 
                                          text="Remove", 
                                          icon='TRASH')

        elif scene.show_texture_map and len(scene.pes_texture_map) == 0:
            box = layout.box()
            box.label(text="No textures loaded", icon='INFO')
            box.operator("pes_flags_trees.add_new_texture_slot", text="Add New Slot", icon='ADD')

        # Selected texture details (minimal preview padding)
        if len(scene.pes_texture_map) > 0 and scene.pes_texture_map_index >= 0:
            selected_texture = scene.pes_texture_map[scene.pes_texture_map_index]
            details_box = layout.box()
            
            # Header with arrow for collapsing
            header_row = details_box.row()
            header_row.prop(scene, "show_texture_details", 
                           text="Selected Texture Details", 
                           icon='TRIA_DOWN' if scene.get("show_texture_details", True) else 'TRIA_RIGHT',
                           emboss=False)
            
            # Only show details if expanded
            if scene.get("show_texture_details", True):
                # Create column with no internal spacing
                main_col = details_box.column()
                main_col.use_property_split = False
                main_col.use_property_decorate = False
                
                # Preview image with ZERO padding/margin
                preview_row = main_col.row()
                preview_row.alignment = 'CENTER'
                # Remove all spacing around the preview
                preview_col = preview_row.column()
                preview_col.separator(factor=0.0)  # No space above
                
                if selected_texture.image and hasattr(selected_texture.image, 'preview') and selected_texture.image.preview:
                    # Direct template_icon with no wrapper elements
                    icon_row = preview_col.row()
                    icon_row.alignment = 'CENTER'
                    icon_row.scale_y = 1.0
                    icon_row.template_icon(icon_value=selected_texture.image.preview.icon_id, scale=5.0)
                else:
                    icon_row = preview_col.row()
                    icon_row.alignment = 'CENTER'
                    icon_row.scale_y = 1.0
                    icon_row.label(text="No Preview", icon='TEXTURE')
                
                preview_col.separator(factor=0.0)  # No space below
                
                # Text information immediately below with normal text size
                info_row = main_col.row()
                
                # Left: Essential info
                left_col = info_row.column()
                left_col.label(text=f"ID: {selected_texture.tex_id}")
                left_col.label(text=f"File: {selected_texture.file_name}")
                
                # Right: Size info
                right_col = info_row.column()
                right_col.alignment = 'RIGHT'
                if selected_texture.image and hasattr(selected_texture.image, 'size') and len(selected_texture.image.size) >= 2:
                    right_col.label(text=f"{selected_texture.image.size[0]}x{selected_texture.image.size[1]}")
                
                try:
                    file_size = os.path.getsize(selected_texture.file_path)
                    size_mb = file_size / (1024 * 1024)
                    right_col.label(text=f"{size_mb:.2f} MB")
                except:
                    right_col.label(text="Unknown")
                
                # Actions and status
                action_row = main_col.row()
                action_row.operator("pes_flags_trees.edit_texture", text="Change", icon='FILEBROWSER')
                action_row.prop(selected_texture, "modified", text="Modified")
                action_row.prop(selected_texture, "new", text="New")
                
                # Error message if no image
                if not selected_texture.image:
                    error_row = main_col.row()
                    error_row.alert = True
                    error_row.label(text="No image", icon='ERROR')

        # Rest of your flag list code...
        layout.prop(scene, "show_flag_list", text="Show Flag List")

        if scene.show_flag_list:
            layout.prop(scene, "flag_search_query", text="Search Flags")
            row = layout.row()
            row.template_list("FLAG_UL_List", "", scene, "flag_list", scene, "flag_list_index", rows=5)

            # Add New Flag button
            layout.operator("pes_flags_trees.add_new_flag", text="Add New Flag", icon='PLUS')

        # Display details of selected flag (if any)
        if len(scene.flag_list) > 0 and scene.flag_list_index >= 0:
            selected_flag = scene.flag_list[scene.flag_list_index]
            box = layout.box()
            box.label(text="Selected Flag Details")
            box.prop(selected_flag, "key", text="Key")
            box.prop(selected_flag, "addr", text="Address")
            box.prop(selected_flag, "flag_type", text="Flag Type")

            row = box.row()
            row.prop(selected_flag, "modified", text="Modified")
            row.prop(selected_flag, "renamed", text="Renamed")
            row.prop(selected_flag, "new", text="New")



# ── Registration ──────────────────────────────────────────────────────────

classes = (
    PIL_PT_installation_panel,
    PES_OT_copy_install_command,
    PES_OT_retry_pil_install,

    TextureMapEntry,
    TEXTURE_UL_List,
    PES_OT_select_texture,
    PES_OT_add_texture,
    PES_OT_remove_texture,
    PES_OT_edit_texture,
    PES_OT_add_new_texture_slot,

    FlagListItem,
    FLAG_UL_List,
    PES_OT_add_new_flag,

    PES_OT_add_flag,
    PES_OT_add_tree,
    PES_OT_apply_flag_texture,

    PES_OT_import_flagarea_fpkd,
    EXPORT_OT_flagarea_fpkd,

    PES_PT_flags_trees_panel,
)


def register():
    # If a previous reload left stale registrations behind, clear them first.
    try:
        unregister()
    except Exception:
        pass

    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.pes_texture_map = CollectionProperty(type=TextureMapEntry)
    bpy.types.Scene.pes_texture_map_index = IntProperty()
    bpy.types.Scene.pes_texture_target_path = StringProperty(
        name="PES Texture Target Path",
        description="Path where PES textures are stored",
        default=""
    )
    bpy.types.Scene.show_texture_map = BoolProperty(
        name="Show Texture Map",
        description="Toggle visibility of the Texture Map",
        default=True
    )
    bpy.types.Scene.show_texture_details = BoolProperty(
        name="Show Texture Details",
        description="Show or hide texture details section",
        default=True
    )

    bpy.types.Scene.flag_list = CollectionProperty(type=FlagListItem)
    bpy.types.Scene.flag_list_index = IntProperty()
    bpy.types.Scene.flag_search_query = StringProperty(name="Search Flags", default="")
    bpy.types.Scene.show_flag_list = BoolProperty(name="Show Flag List", default=True)

    bpy.types.Scene.pes_flagarea_export_path = StringProperty(
        name="Flagarea Export Path",
        description=r"Path to flagarea_st0##.fpk inside the stadium's standsFlag\#Win\ folder",
        subtype='FILE_PATH',
        default=""
    )


def unregister():
    for name in (
        "pes_flagarea_export_path",
        "show_flag_list", "flag_search_query", "flag_list_index", "flag_list",
        "show_texture_details", "show_texture_map",
        "pes_texture_target_path", "pes_texture_map_index", "pes_texture_map",
    ):
        if hasattr(bpy.types.Scene, name):
            try:
                delattr(bpy.types.Scene, name)
            except Exception:
                pass

    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass


if __name__ == "__main__":
    register()
