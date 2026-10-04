# SPDX-License-Identifier: Apache-2.0
"""Band arithmetic: difference, ratio, a formula, and a threshold mask.

Hardware: none (a scene recorded on the simulated device).
    python examples/12_expression_ratio_threshold.py [-o work]
"""

import argparse
import ast
import operator
from pathlib import Path

import numpy as np

import vispek_hc

EPSILON = 1e-8
OPERATORS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
             ast.Div: lambda a, b: a / np.where(np.abs(b) > EPSILON, b, np.nan)}  # fmt: skip
FUNCTIONS = {"abs": np.abs, "sqrt": np.sqrt, "log": np.log, "exp": np.exp,
             "minimum": np.minimum, "maximum": np.maximum}  # fmt: skip


def evaluate(formula: str, cube: vispek_hc.Cube) -> np.ndarray:
    """A formula over band names (led_05 ...), numbers, + - * / and a few functions.

    The expression is parsed, not executed: anything else in it is refused.
    """

    def value(node: ast.expr) -> np.ndarray | float:
        if isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
            return OPERATORS[type(node.op)](value(node.left), value(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -value(node.operand)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return float(node.value)
        if isinstance(node, ast.Name):
            return cube.values[:, :, cube.band_index(node.id)]
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in FUNCTIONS
            and not node.keywords
        ):
            return FUNCTIONS[node.func.id](*map(value, node.args))
        raise ValueError(f"not allowed in a formula: {ast.dump(node)[:40]}")

    return np.asarray(value(ast.parse(formula, mode="eval").body), np.float32)


parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("-o", "--output", type=Path, default=Path("work"))
args = parser.parse_args()

sample, white = vispek_hc.example_captures(args.output)
cube = vispek_hc.apply_calibration(sample, vispek_hc.make_calibration([white]))

difference = evaluate("led_13 - led_09", cube)
ratio = evaluate("led_13 / led_09", cube)
index = evaluate("(led_12 - led_10) / (led_12 + led_10)", cube)
dark_parts = cube.values[:, :, cube.band_index("led_09")] < 0.8  # everything but the board

print("difference 850-630: median", round(float(np.nanmedian(difference)), 4))
print("ratio 850/630:      median", round(float(np.nanmedian(ratio)), 4))
print("formula (NDVI):     median", round(float(np.nanmedian(index)), 4))
print("pixels below 0.8 at 630 nm:", int(dark_parts.sum()), "of", dark_parts.size)
classes = vispek_hc.views.colourise(dark_parts.astype(np.uint8), "classes")
print("wrote", vispek_hc.save_png(classes, args.output / "threshold.png"))
for formula in ("__import__('os').getcwd()", "led_09 * True", "abs(led_09, out=led_09)"):
    try:
        evaluate(formula, cube)
    except (ValueError, vispek_hc.VispekHCError) as error:
        print("refused:", str(error)[:60])
