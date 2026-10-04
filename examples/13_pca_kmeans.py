# SPDX-License-Identifier: Apache-2.0
"""Principal components and K-means clusters of the spectra.

Hardware: none (a scene recorded on the simulated device). K-means needs scikit-learn:
install with the 'analysis' extra; without it only the PCA part runs.
    python examples/13_pca_kmeans.py [-o work] [--clusters 3]
"""

import argparse
from pathlib import Path

import numpy as np

import vispek_hc

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("-o", "--output", type=Path, default=Path("work"))
parser.add_argument("--clusters", type=int, default=3)
args = parser.parse_args()

sample, white = vispek_hc.example_captures(args.output)
cube = vispek_hc.apply_calibration(sample, vispek_hc.make_calibration([white]))

# PCA is fitted on the pixels that are valid in every band; mean removed, not scaled.
scores = vispek_hc.principal_components(cube)
usable = np.isfinite(scores).all(axis=2)
print("pixels used:", int(usable.sum()), "of", usable.size)
print("variance of the first three components:", scores[usable].var(axis=0).round(5).tolist())
print("wrote", vispek_hc.save_png(vispek_hc.render(cube, "pca"), args.output / "pca.png"))

try:
    from sklearn.cluster import KMeans
except ImportError:
    print("scikit-learn is not installed: skipping K-means (pip install 'vispek-hc[analysis]')")
else:
    # Same pixels, features are the relative values as they are. Fixed seed, one start.
    model = KMeans(n_clusters=args.clusters, random_state=42, n_init=1).fit(cube.values[usable])
    labels = np.full(usable.shape, 255, np.uint8)  # 255 marks pixels that were left out
    labels[usable] = model.labels_
    print("cluster sizes:", np.bincount(model.labels_, minlength=args.clusters).tolist())
    image = vispek_hc.views.colourise(labels, "classes")
    print("wrote", vispek_hc.save_png(image, args.output / "clusters.png"))
