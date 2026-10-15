# GNU FriBidi 1.0.17 (Windows DLL)

`libfribidi-0.dll` is GNU FriBidi 1.0.17, the Unicode bidirectional algorithm library, unmodified. It is licensed
under the **GNU LGPL 2.1 or later** (`COPYING.txt`). Source code: https://github.com/fribidi/fribidi (tag v1.0.17).

Pillow's text layout (raqm + HarfBuzz, bundled in the Pillow wheel) loads it at run time, so Devanagari, Arabic,
Hebrew and other complex scripts are shaped correctly (conjuncts, joined letters, right-to-left order). It is a
separate, replaceable library: put any compatible `libfribidi-0.dll` here instead.

Binary from the MSYS2 package `mingw-w64-ucrt-x86_64-fribidi-1.0.17-1-any.pkg.tar.zst`
(https://repo.msys2.org/mingw/ucrt64/), whose SHA-256 3581ee03cf83a9bb0e98bf688c4f532a29553500e317532d90746de295b0f8a8
matched the repository database. DLL SHA-256: see `backend/app/text.py` (`FRIBIDI_SHA256`).
