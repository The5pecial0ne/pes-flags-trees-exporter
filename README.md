# PES Flags & Trees

A Blender addon that lets you add animated corner-flags and trees to a PES
2021 stadium mod, then export them straight into the game files — no
manual file-editing needed.

Point Blender at your stadium's `flagarea` file, place some flags and
trees where you want them, hit Export, and you're done. If you need to go
back and change something later, you can re-import the same file and pick
up right where you left off.

## Where this comes from

This addon is built on the flags & trees feature originally created by
**PallasDav** for [PES Light Manager v0.2.0d](https://github.com/PallasDav/pes_lightmanager/releases/tag/v.0.2.0d).
That version bundled flags/trees together with a full lighting toolkit, all
in one addon. This project takes just the flags & trees part and ports it
forward as its own separate, lighter tool — same underlying logic, models
and export pipeline, just packaged on its own so you don't need the rest
of the lighting toolkit if all you want is flags and trees. All credit for
the original feature goes to PallasDav.

## What you can do with it

- **Add flags** in three sizes (small, medium, large), right in the
  viewport.
- **Add trees** in two styles.
- **Give a flag its own look** by applying a custom `.dds` texture image
  to it.
- **Export everything** into your stadium's `flagarea.fpk` / `.fpkd`
  files — the addon handles all the behind-the-scenes file conversion for
  you.
- **Come back and edit later**: import an already-exported `flagarea.fpkd`
  and every flag/tree in it appears in Blender again, ready to move,
  resize, or add to. Export again when you're happy.
- Works on **Windows, macOS, and Linux**.

## Before you install

1. **Blender 2.93 or newer.**
2. **Pillow** (a Python image library) needs to be installed into
   Blender's *own* copy of Python — not your regular system Python. If
   it's missing, the addon will tell you and show you the exact command to
   run. It looks something like this (the path will differ on your
   computer):
   ```
   "C:\Program Files\Blender Foundation\Blender\4.2\python\bin\python.exe" -m pip install pillow
   ```
3. **Mac and Linux users only:** you also need [Mono](https://www.mono-project.com/download/stable/)
   installed, because the file-conversion tools this addon uses were
   originally built for Windows. (Windows users can skip this — nothing
   extra to install.)

## Installing the addon

1. Download the addon `.zip` file (don't unzip it).
2. Open Blender, go to **Edit > Preferences > Add-ons > Install...**
3. Select the `.zip` file you downloaded.
4. Tick the checkbox next to **"PES Flags & Trees"** to enable it.
5. You'll now find the tool under **Properties panel > Scene tab > PES
   Flags & Trees**.

**Tip for Mac users:** the first time you use the export or import
feature, open Blender by running it from Terminal instead of double-
clicking the app icon:
```
/Applications/Blender.app/Contents/MacOS/Blender
```
This way, if anything goes wrong, you'll actually see the error message
in the Terminal window instead of nothing happening silently.

## How to use it

1. **Add some flags or trees.** In the PES Flags & Trees panel, click
   "+ Flag" or "+ Tree" — a new object appears at the 3D cursor. Move,
   rotate, or scale it like any normal Blender object.
2. **(Optional) Give a flag a custom texture.** Select the flag, then
   click "Apply Texture to Selected Flag(s)" and pick a `.dds` image.
3. **Export.** Point the export field at your stadium's
   `flagarea_st0##.fpk` file (inside its `standsFlag\#Win\` folder), then
   click Export. The addon takes care of packing everything back into the
   game's file format.
4. **Editing later?** Click "Import Flagarea FPKD" and select the
   `.fpkd` file you exported earlier. Every flag and tree you placed will
   reappear in the viewport, and the export field fills in automatically
   — no need to browse for the file again. Make your changes and export
   as before.

## A couple of things to know

- If a stadium's file layout doesn't follow the standard
  `standsFlag\#Win\` folder pattern, the export step may not find your
  texture files.
- This addon has been tested through a full add → export → re-import
  cycle on macOS. Windows should work the same way (the file tools run
  natively there), but hasn't been separately confirmed.
- This is a fan-made tool for modding purposes, built on PallasDav's
  original work and shared in that same spirit.
