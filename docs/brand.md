# Seam logo

The owner selected concept 01, **Joined S**, on 2026-10-07. The teal upper ribbon
and ice-white lower ribbon form an S, joined across a diagonal seam, on a rounded
navy tile. The icon is `vscode/images/icon.png`; the extension uses it through its
manifest's `icon` field.

The concept and final PNG were made with the built-in imagegen tool. The final
editing prompt preserved the approved large icon's S silhouette, diagonal seam,
proportions, orientation, colours and internal padding; removed the presentation
canvas, smaller variants, labels and wordmark; and requested a standalone square
PNG with transparency outside the rounded tile. No third-party language logos are
included.

Extension 0.1.1 is a presentation update carrying the unchanged released debugger
0.1.0. The manifest explicitly records `seamAdapterVersion`; package construction
and release verification check that this matches the adapter actually included.

## Debugging demo

`vscode/images/python-rust-demo.gif` is a real VS Code recording of the packaged
extension 0.1.1 with debugger 0.1.0. It shows the PyO3 example stopping in Python,
Step Into entering Rust, the Rust loop paused with `i = 2` and `total = 1`,
Step Out returning to Python, and the completed assignment with `result = 30`.
The program prints `squares(5) = 30` and exits successfully.

The recording used VS Code 1.141.0, CPython 3.12 and LLDB 20 on a private Xvfb
display in WSL. An empty extension-host driver operated VS Code's debug commands
and checked the adapter's stack, variables and exit response. The debugger itself
came from the installed VSIX. No simulated editor screens are included.

The GIF is 1280 by 800 pixels, about 25 seconds long, at 10 frames per second.
Recording starts at the first Python breakpoint. Pauses give viewers time to read
the variables; the clip is not a startup benchmark. Capture scripts, the original
MP4 and five inspected screenshots remain in the ignored `build/demo-capture/`
directory. The GIF joins extension 0.1.2 with the revised introduction and
installation instructions. This package keeps the released debugger 0.1.0.
