# Provenance

The code in this repository is an independent implementation. It was written from a
functional specification, from Vispek's own hardware documents, and from public technical
references. No source code, screenshots or documentation of any earlier or third-party
camera software were consulted while writing it, and none is included here.

## Inputs read during implementation

Vispek's own material:

- The functional specification of this project (internal document; describes functions,
  protocol, data and observed hardware facts, not code).
- The light-source serial protocol sheet (frame layout, CRC-8 reference routine, worked
  examples, and a 34-frame routine for LEDs 1-17). Its examples are the test vectors in
  `tests/test_protocol.py`.
- The HC1500 technical data sheet (wavelength list, interfaces, camera module figures).
- Sample frames captured with an HC1500 on 2026-09-16, with their per-frame metadata
  (data, not code). They are not distributed; maintainers use them to check that the
  processing reproduces the reference values of the specification
  (`tests/test_sample_data.py`).

Public references:

- Python standard library documentation: <https://docs.python.org/3/>
- pyserial: <https://pyserial.readthedocs.io/>
- NumPy: <https://numpy.org/doc/>
- Pillow: <https://pillow.readthedocs.io/>
- Hypothesis: <https://hypothesis.readthedocs.io/>
- FFmpeg documentation (devices `avfoundation`, `v4l2`, `dshow`; `-fps_mode`;
  `mjpeg2jpeg` bitstream filter): <https://ffmpeg.org/documentation.html>
- uvc-util by Jeffrey Frey (MIT), source and README; vendored unmodified under
  `tools/uvc-util/`: <https://github.com/jtfrey/uvc-util>
- ITU-R BT.601 and the JFIF convention for full-range YCbCr to RGB conversion.
- ITU-T T.81 (JPEG) for the marker segment structure used to split MJPEG streams.
- The ENVI header format as documented by NV5 / L3Harris, and its reading by GDAL.
- JSON Schema 2020-12: <https://json-schema.org/>
- scikit-learn (K-means, used by one example): <https://scikit-learn.org/>
- Colour values of the display tables in `vispek_hc.views`: nine samples of matplotlib's
  viridis colour map (CC0), the ten-class "Spectral" scheme of ColorBrewer 2.0 by Cynthia
  Brewer and Mark Harrower (Apache-2.0), and the twenty category colours published by
  Tableau and D3 ("category20"). Only the numbers were taken; the interpolation is ours.
  See `THIRD-PARTY-NOTICES.md`.

This list grows with each milestone; every new input is added here in the same change
that first uses it.

## Naming

Module, class, function, variable and error-code names were chosen for this codebase.
`tests/test_provenance.py` fails the build if identifiers known to belong to an unrelated
earlier implementation appear under `src/`, `docs/` or `examples/`.

## Contributions

Contributors must not introduce code derived from proprietary or licence-incompatible
camera software. See `CONTRIBUTING.md`.
