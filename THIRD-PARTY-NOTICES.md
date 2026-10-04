# Third-party notices

## Source code included in this repository

| Component | Licence | Where |
|---|---|---|
| [uvc-util](https://github.com/jtfrey/uvc-util), Copyright (c) 2016 Jeffrey Frey | MIT | `tools/uvc-util/` (unmodified, commit `8110da7025c95eea3096a7181af9a46c0cc7ac37`; licence text in `tools/uvc-util/LICENSE`) |

Its source files are part of the Python wheel (`vispek_hc/_uvc_util/`, with its licence);
no binary is distributed. `vispek-hc setup` compiles it on the user's Mac.

## Colour tables

`src/vispek_hc/views.py` holds three small tables of RGB values:

| Table | From | Licence |
|---|---|---|
| `viridis` (9 anchor colours) | the viridis colour map of [Matplotlib](https://matplotlib.org/), by Nathaniel Smith and Stefan van der Walt | CC0 |
| `spectral` (10 anchor colours) | the "Spectral" scheme of [ColorBrewer 2.0](https://colorbrewer2.org/) | Apache-2.0 |
| `classes` (20 colours) | the "category20" colours published by Tableau and used by [D3](https://d3js.org/) | D3: BSD-3-Clause |

This product includes color specifications and designs developed by Cynthia Brewer
(https://colorbrewer2.org/). Copyright (c) 2002 Cynthia Brewer, Mark Harrower, and The
Pennsylvania State University.

## Run-time dependencies

| Component | Licence | How it is used |
|---|---|---|
| [NumPy](https://numpy.org/) | BSD-3-Clause | required dependency |
| [pyserial](https://github.com/pyserial/pyserial) | BSD-3-Clause | required dependency |
| [Pillow](https://python-pillow.org/) | MIT-CMU | required dependency |
| [OpenCV](https://opencv.org/) (`opencv-python`) | Apache-2.0 | optional extra `[opencv]` |
| [Matplotlib](https://matplotlib.org/) | Matplotlib licence (BSD-style) | optional extra `[plot]` |
| [scikit-learn](https://scikit-learn.org/) | BSD-3-Clause | optional extra `[analysis]` |

FFmpeg is not distributed with this project. If you install it yourself, its licence
(LGPL or GPL) depends on how your copy was built.
