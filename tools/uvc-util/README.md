# uvc-util (vendored)

`vispek-hc` uses this small macOS tool to set and read back the camera's UVC parameters
(exposure, white balance, gain, ...). Without it a scan cannot prove that the camera
stayed in manual mode.

- Upstream: <https://github.com/jtfrey/uvc-util>, by Jeffrey Frey, MIT licence (see `LICENSE`).
- Vendored commit: `8110da7025c95eea3096a7181af9a46c0cc7ac37` (2024-02-26).
- The seven files in `src/` and `LICENSE` are byte-for-byte copies; `SHA256SUMS` lists
  their hashes and `tests/test_vendor.py` checks them. Do not edit them here: send
  changes upstream and re-vendor.

## Build

macOS with the Xcode command line tools (`xcode-select --install`):

```bash
tools/uvc-util/build.sh
```

The binary lands in `tools/uvc-util/build/uvc-util` (not committed). `vispek-hc` looks
for it, in this order: the `VISPEK_HC_UVC_UTIL` environment variable,
`<VISPEK_HC_HOME>/bin/uvc-util`, this build directory of a source checkout, then `PATH`.
If you installed `vispek-hc` from a wheel, copy the binary to `<VISPEK_HC_HOME>/bin/`;
`vispek-hc check` prints where that is.
