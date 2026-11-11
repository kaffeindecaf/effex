# effect reference

every effex effect with its options, examples and implementation notes.
back to the [readme](../README.md), or see them all running in the
[gallery](../README.md#gallery).

30 effects, grouped the way the picker groups them. every one keeps the source
frame size and audio unless its own section says otherwise.

| effect | what it does | chains |
| --- | --- | --- |
| `stretch` | squash the frame into another shape | yes |
| `pixelize` | turn the picture into fat blocks | yes |
| `zoom` | creep slowly into the frame | yes |
| `wobble` | rock the frame side to side | yes |
| `spin` | spin the whole frame | yes |
| `stutter` | chop the frame rate down | yes |
| `slowmo` | speed the clip up or down, audio pitches with it | yes |
| [`collapse`](#content-aware-elastic-collapse) | progressive content-aware scale, optional pixel mosh | yes |
| [`ripple`](#ripple-tank) | movement splashes into water that bends the picture | yes |
| `rgbsplit` | drift the color planes apart | yes |
| `deepfry` | saturate and crunch it to death | yes |
| `posterize` | crush the colors into bands | yes |
| `channelswap` | circuit-bent rgb - channels trade places | yes |
| `vhs` | tape grain, color bleed, tracking tears | yes |
| `crt` | scanlines, fuzz and a slow roll | yes |
| [`lobotomy`](#channel-lobotomy) | crush, kill and swap individual color channels | yes |
| [`neon`](#neon-hue-cycle) | swing through saturated acid colors with a contrast punch | yes |
| `trails` | frames bleed into each other | yes |
| `timescratch` | drag the timeline cursor - forward, backward, slow, fast | own pipeline |
| `sediment` | deposit frames into bounded, decaying visual history | yes |
| `dropout` | frames vanish at random - the picture holds | yes |
| [`inversion`](#causality-inversion) | borrow real pixels from buffered future frames | yes |
| [`archaeology`](#pixel-archaeology) | borrow a cutout from the future, then melt it away | yes |
| [`rasterslip`](#raster-slip) | travelling scan bands tear sideways through different past moments | yes |
| [`datamosh`](#heavy-datamosh) | codec-level melt - the decoder smears the motion | own pipeline |
| [`blockmelt`](#block-melt) | macroblocks drag the stale picture along the motion | yes |
| [`blockswap`](#blockswap) | blocks trade places, teleport on a timer, optional mosh | own pipeline |
| [`flowmosh`](#flow-mosh) | optical flow drags stale pixels - a pixel-domain datamosh | yes |
| [`bitrot`](#bitrot) | corrupt the codec bitstream - broken macroblocks drift | own pipeline |
| `crunch` | turn the bitrate and resolution down | yes |

## shape and motion

<details>
<summary><b>stretch</b> - squash the frame into another shape</summary>

| option | meaning | default | range |
| --- | --- | --- | --- |
| `target` | shape of the frame | 9:16 | `9:16`, `16:9`, `4:5`, `1:1`, `3:4`, `custom` |
| `resample` | how pixels blend when scaling | bicubic | `bicubic`, `bilinear`, `neighbor`, `lanczos`, `spline` |
| `rotation` | strip phone rotation from the file | on | on, off |
| `flip` | mirror the frame after the squash | none | `none`, `h`, `v`, `hv` |

```sh
./effex.py stretch input/clip.mp4
```

</details>

<details>
<summary><b>pixelize</b> - turn the picture into fat blocks</summary>

| option | meaning | default | range |
| --- | --- | --- | --- |
| `chunk` | bigger = chunkier, in pixels per block | 24 | 2-128 |
| `edges` | block edges: crisp pixels or soft blur | crisp | `crisp`, `soft` |
| `shape` | shape of the frame | keep | `keep`, `9:16`, `16:9`, `4:5`, `1:1`, `3:4`, `custom` |

```sh
./effex.py pixelize input/clip.mp4
```

</details>

<details>
<summary><b>zoom</b> - creep slowly into the frame</summary>

| option | meaning | default | range |
| --- | --- | --- | --- |
| `zoom` | how far in it creeps (percent of the frame) | 135 | 110-300 |
| `speed` | how fast it creeps | slow | `slow`, `fast` |

```sh
./effex.py zoom input/clip.mp4
```

</details>

<details>
<summary><b>wobble</b> - rock the frame side to side</summary>

| option | meaning | default | range |
| --- | --- | --- | --- |
| `amount` | how far the frame rocks, in degrees | 4 | 1-15 |
| `speed` | how fast it rocks | slow | `slow`, `medium`, `fast` |
| `waveform` | the shape of the rock | sine | `sine`, `triangle` |

```sh
./effex.py wobble input/clip.mp4
```

</details>

<details>
<summary><b>spin</b> - spin the whole frame</summary>

| option | meaning | default | range |
| --- | --- | --- | --- |
| `period` | time for one full rotation | 6.0 | 0.5-30.0 |
| `direction` | which way it turns | clockwise | `clockwise`, `counterclockwise` |

```sh
./effex.py spin input/clip.mp4
```

</details>

<details>
<summary><b>stutter</b> - chop the frame rate down</summary>

| option | meaning | default | range |
| --- | --- | --- | --- |
| `fps` | how choppy the motion gets | 8 | `8`, `12`, `15` |
| `mode` | how the gap is filled | drop | `drop`, `hold` |

```sh
./effex.py stutter input/clip.mp4
```

</details>

<details>
<summary><b>slow + pitch</b> - speed the clip up or down, audio pitches with it</summary>

| option | meaning | default | range |
| --- | --- | --- | --- |
| `factor` | how slow or fast it plays | 0.5 | `0.25`, `0.5`, `2`, `4` |

```sh
./effex.py slowmo input/clip.mp4
```

</details>

### content-aware elastic collapse

`collapse` lives under **SHAPE & MOTION**. The default `seam` mode behaves like
Photoshop content-aware scaling: each frame starts at its original composition,
then its content-aware shrink grows linearly from the first frame to the clip's
end. Low-energy seams are removed from a reduced analysis frame. The renderer
inverts that seam-removal displacement so the content shrinks, then maps it to
the original canvas dimensions. High-gradient regions resist shrinking.
Analysis is capped at 192 pixels on the longest side and updates at most five
times per second; the displacement field is smoothed between updates.
`mode=elastic` keeps the earlier seeded, spatially nonuniform elastic warp.
Pixelation is a separate switch and is **off by default**: the render is the
smooth warped frame at source resolution. Set `pixelate=on` to finish with a
reduced-resolution grid enlarged using nearest neighbors, and optionally let
moving regions borrow a limited blend of the previous frame's blocks for a
codec-mosh-like trail. The effect never modifies codec packets.

| option | range / values | default |
| --- | --- | --- |
| `intensity` | 0-100%, overall scale strength (elastic warp strength in elastic mode) | 100% |
| `horizontal` | 0-100%, share of the maximum width shrink / horizontal warp | 100% |
| `vertical` | 0-100%, share of the maximum height shrink / vertical warp | 100% |
| `preserve` | 0-100%, content/edge protection | 75% |
| `mode` | seam, elastic | seam |
| `seam_strength` | 0-100%, maximum content-aware shrink by clip end | 100% |
| `motion` | 0-100%, elastic motion influence | 50% |
| `smoothness` | 0-100%, spatial field smoothness | 70% |
| `speed` | 0-4 elastic cycles per second | 0.35 |
| `coherence` | 0-100%, elapsed-time field smoothing | 80% |
| `seed` | 0-2147483647; elastic mode only, 0 derives phases from the source | 0 |
| `pixelate` | on, off - finish with chunky pixel blocks | off |
| `block_size` | 1-16 reduced-grid cells when `pixelate=on`; larger means chunkier | 4 |
| `mosh` | 0-100%, previous-block carry when `pixelate=on` (up to 50% mix) | 60% |

In seam mode, shrink is capped at 50% per axis. The target reduction is
`50% × intensity × seam_strength × horizontal/vertical control`; `preserve`
steers seams away from strong edges. The final displacement is multiplied by
elapsed clip progress, producing a linear ramp while each output frame remains
at source dimensions. `smoothness` and `coherence` steady the reduced-resolution
seam map; motion, speed, and random phases apply only to elastic mode.

Zero intensity is an exact passthrough; two zero axis scales disable both modes.
`block_size` and `mosh` do nothing while `pixelate=off`; the smooth remap is the
default look.
Zero seam strength disables seam mode. Seam geometry starts at identity and
reaches its configured scale at clip end.
State resets on a new input/shape, explicit reset, rewind, frame discontinuity,
explicit scene cut, or a large RGB scene change. Persistent analysis state is
bounded independently of clip length; full-frame remap buffers are transient.
Chains such as `neon+collapse+sediment+deepfry` reuse the shared decoder,
encoder, audio handling, progress, cancellation and partial-file cleanup.

Benchmark both modes, with and without pixelation (requires the optional
dependencies):

```sh
python3 tests/benchmark_collapse.py --frames 60 --output benchmarks/collapse.json
python3 tests/benchmark_collapse.py --frames 60 --pixelate on --output benchmarks/collapse-pixelated.json
```

The report measures processor frame time and peak process RSS at 640x360 and
1280x720 in fresh workers. It excludes FFmpeg decode/encode and is not an
end-to-end playback guarantee. Seam mode also reports carving-update time.

Recorded on this Linux x86_64 host with one OpenCV CPU thread, 60 frames per
case, Python 3.13.5, NumPy 2.2.4 and OpenCV 4.12.0 (raw data in
`benchmarks/collapse.json` and `benchmarks/collapse-pixelated.json`):

| mode | pixelate | frame size | mean ms/frame | p95 ms/frame | peak process RSS |
| --- | --- | --- | --- | --- | --- |
| elastic | off | 640x360 | 4.41 | 4.55 | 64.5 MiB |
| elastic | off | 1280x720 | 10.51 | 10.74 | 109.8 MiB |
| seam | off | 640x360 | 16.71 | 89.15 | 63.6 MiB |
| seam | off | 1280x720 | 26.34 | 114.34 | 110.2 MiB |
| elastic | on | 640x360 | 4.53 | 4.63 | 64.9 MiB |
| elastic | on | 1280x720 | 11.90 | 12.15 | 110.1 MiB |
| seam | on | 640x360 | 22.30 | 122.10 | 63.4 MiB |
| seam | on | 1280x720 | 28.22 | 114.30 | 110.2 MiB |

Seam averages include frames reusing a cached map, so p95 is dominated by the
carving update. Skipping pixelation removes the extra downsample/upsample pass
(about 5 ms mean in seam mode here) and is the default. These are measurements
of this fixture on this host, not a promise for every input.

### ripple tank

`ripple` sits under **SHAPE & MOTION**. It lays a simulated water surface over
the clip and lets the footage disturb it: wherever the picture changes, the
frame difference pushes the surface down, and the waves spread out, bounce off
the frame edges and slowly calm. Optional rain drops random stones on top.
The surface then acts like a lens. Each pixel is sampled along the slope of
the water, so edges wobble and smear around anything that moves. Red and blue
refract by slightly different amounts (a prism fringe), and curved parts of
the surface focus light into bright and dark caustic patterns.

```sh
./effex.py ripple input/clip.mp4
./effex.py ripple input/clip.mp4 --set rain=8 --set ring=95 --set depth=30
./effex.py ripple input/clip.mp4 --set strength=0 --set rain=12 --set caustics=80
./effex.py neon+ripple+crt input/clip.mp4 --set ripple.prism=60
```

| option | meaning | default | range |
| --- | --- | --- | --- |
| `strength` | how hard movement pushes the water | 60% | 0-100% |
| `threshold` | brightness change a pixel needs before it makes waves; raise it for noisy footage | 6% | 0-50% |
| `rain` | random drops per second | 1.5 | 0-20 |
| `depth` | refraction strength, in pixels per unit of surface slope | 14 | 0-60 |
| `ring` | how long waves keep travelling before they die out | 85% | 0-100% |
| `speed` | grid cells a wave crosses each frame | 0.45 | 0.05-0.7 |
| `prism` | how differently red and blue bend | 20% | 0-100% |
| `caustics` | light focusing where the surface curves | 40% | 0-100% |
| `seed` | repeatable rain; 0 derives from source | 0 | 0-2147483647 |

The first frame starts clean, and still footage without rain is left exactly
as it was. With `prism` and `caustics` at zero, every output pixel is a real
pixel from the current frame, just moved. Zero `strength` and `rain`, or zero
`depth` and `caustics`, passes frames through untouched. Scene cuts, rewinds,
frame-index jumps and new sources calm the water.

The wave grid is capped at 360 cells on its longest side and upsampled
smoothly, so state stays under a couple of MiB at any resolution and the wave
pattern keeps its shape at 360p and 4K (`depth` is in pixels, so raise it for
big frames). Surface height is clamped, so even
maximum settings cannot blow up. Waves move in frames, not seconds: a
60 fps clip ripples twice as fast as the same clip at 30 fps. Requires NumPy
from `requirements-stateful.txt`. It chains with ordinary filters and other
stateful effects; there is no stateful preview.

## color and texture

<details>
<summary><b>rgb split</b> - drift the color planes apart</summary>

| option | meaning | default | range |
| --- | --- | --- | --- |
| `amount` | how far the color planes drift (chroma steps) | 12 | 2-80 |
| `axis` | which way the planes drift | horizontal | `horizontal`, `vertical` |
| `channels` | which planes drift (pick several) | ['r', 'b'] | `r`, `b` |

```sh
./effex.py rgbsplit input/clip.mp4
```

</details>

<details>
<summary><b>deepfry</b> - saturate and crunch it to death</summary>

| option | meaning | default | range |
| --- | --- | --- | --- |
| `strength` | how fried (saturation, contrast, crunch) | 40 | 10-100% |
| `brightness` | lift or crush the exposure | 0 | -30-30 |
| `crisp` | how crunchy the sharpening gets | 20 | 0-20 |

```sh
./effex.py deepfry input/clip.mp4
```

</details>

<details>
<summary><b>posterize</b> - crush the colors into bands</summary>

| option | meaning | default | range |
| --- | --- | --- | --- |
| `levels` | fewer = harsher color bands | 8 | `2`, `4`, `8`, `16` |
| `grain` | sprinkle noise to break up the bands | off | on, off |

```sh
./effex.py posterize input/clip.mp4
```

</details>

<details>
<summary><b>channelswap</b> - circuit-bent rgb - channels trade places</summary>

| option | meaning | default | range |
| --- | --- | --- | --- |
| `swap` | which channels trade places | rb | `rb`, `gb`, `rg`, `roll`, `invert` |
| `bleed` | how far the color smears sideways | 0 | 0-30 |

```sh
./effex.py channelswap input/clip.mp4
```

</details>

<details>
<summary><b>vhs</b> - tape grain, color bleed, tracking tears</summary>

| option | meaning | default | range |
| --- | --- | --- | --- |
| `bleed` | how far the color smears sideways | 4 | 0-40 |
| `noise` | how much tape grain | 14 | 0-60 |
| `tracking` | vertical roll tears, in pixels | 12 | 0-40 |
| `fade` | wash the colors out like an old tape | on | on, off |

```sh
./effex.py vhs input/clip.mp4
```

</details>

<details>
<summary><b>crt</b> - scanlines, fuzz and a slow roll</summary>

| option | meaning | default | range |
| --- | --- | --- | --- |
| `lines` | dark lines across the screen | on | on, off |
| `curve` | bulge the screen like a tube | off | on, off |
| `noise` | how much electric fuzz | 6 | 0-30 |
| `roll` | a slow vertical crawl, in pixels | 0 | 0-30 |

```sh
./effex.py crt input/clip.mp4
```

</details>

### channel lobotomy

`lobotomy` crushes each RGB channel independently, removes an optional source
channel, then swaps the survivors. Find it under **COLOR & TEXTURE** in the menu.

| option | values | default |
| --- | --- | --- |
| `red` | 2, 4, 8, 16, 32, 64, 128, 256 levels | 8 |
| `green` | same level choices | 4 |
| `blue` | same level choices | 8 |
| `kill` | none, r, g, b | g |
| `swap` | none, rb, rg, gb, roll | rb |

The filter order is `format=rgb24,lutrgb,colorchannelmixer,colorchannelmixer`:
quantization uses `floor(val/Q)*Q` with `Q=256/levels`; kill happens **before**
swap, so killing red and swapping red/blue leaves the output blue channel empty.
Set all three levels to 256 with `kill=none` and `swap=none` for unchanged colors.

### neon hue cycle

`neon` swings the hue back and forth, boosts saturation, and punches contrast
with an S-curve. It is also under **COLOR & TEXTURE**.

| option | range | default |
| --- | --- | --- |
| `amount` | 0-180 degrees of hue swing | 120 |
| `period` | 0.5-30 seconds per full cycle | 4 |
| `saturation` | 100-300% | 200% |
| `punch` | 0-100% curve strength | 50% |

The filter is `hue=h='A*sin(2*PI*t/P)':s=S,curves=all='...'`.
`h` uses degrees, not radians. Saturation at 100% is unchanged; punch at 0%
disables the curve. Set `amount=0`, `saturation=100` and `punch=0` to disable
the whole effect. Both effects preserve frame size and audio, support `p`
previews, and chain in either order without extra dependencies.

## time and memory

<details>
<summary><b>trails</b> - frames bleed into each other</summary>

| option | meaning | default | range |
| --- | --- | --- | --- |
| `trail` | how many frames bleed into each other | 10 | 2-20 |
| `mode` | even blend or a fading tail | fade | `fade`, `even` |

```sh
./effex.py trails input/clip.mp4
```

</details>

<details>
<summary><b>timescratch</b> - drag the timeline cursor - forward, backward, slow, fast</summary>

| option | meaning | default | range |
| --- | --- | --- | --- |
| `curve` | the math that generates the scratches | sine | `sine`, `random`, `swing`, `walk` |
| `speed` | how wild the speeds get | rough | `gentle`, `rough`, `extreme` |
| `cuts` | how many scratches across the clip | medium | `light`, `medium`, `heavy` |

```sh
./effex.py timescratch input/clip.mp4
```

</details>

<details>
<summary><b>temporal sediment</b> - deposit frames into bounded, decaying visual history</summary>

| option | meaning | default | range |
| --- | --- | --- | --- |
| `intensity` | how much accumulated history shows | 65 | 0-100% |
| `deposit` | how strongly new frames enter history | 70 | 0-100% |
| `decay` | how quickly old layers fade | 35 | 0-100% |
| `layers` | number of decay speeds to retain | 3 | `1`, `2`, `3` |
| `motion` | how strongly movement deposits pixels | 50 | 0-100% |
| `excavation` | chance per frame to reveal an older patch | 5 | 0-100% |
| `excavation_size` | patch width and height as a frame percent | 15 | 5-50% |
| `color` | how stored colors are treated | keep | `keep`, `warm`, `cool`, `mono` |
| `seed` | 0 derives a repeatable seed from the clip | 0 | 0-2147483647 |

```sh
./effex.py sediment input/clip.mp4
```

</details>

<details>
<summary><b>dropout</b> - frames vanish at random - the picture holds</summary>

| option | meaning | default | range |
| --- | --- | --- | --- |
| `chance` | percent of frames that vanish | 8 | 1-50 |

```sh
./effex.py dropout input/clip.mp4
```

</details>

### causality inversion

`inversion` is under **TIME & MEMORY**. Selected regions borrow actual pixels
from a later decoded frame. It never predicts or fabricates future content.

| option | range / values | default |
| --- | --- | --- |
| `lookahead` | 0-5 seconds of buffer horizon | 0.5 |
| `offset` | 0-lookahead seconds into the future | 0.5 |
| `strength` | 0-100%, opacity or replacement coverage | 65% |
| `mode` | blend, replace, tear, align | blend |
| `region` | full, bands, blocks, patch | full |
| `mask_speed` | 0-4 mask/tear cycles per second | 0.25 |
| `alignment` | 0-100%, flow gain in align mode | 100% |
| `feather` | 0-100%, region edge softness | 10% |
| `seed` | 0-2147483647; 0 derives masks from source | 0 |

Blend mixes current and future RGB values. Replace uses strength as seeded
patch coverage, so half strength replaces some pixels rather than making all
pixels half-transparent. Tear shifts horizontal bands from the future image.
Align estimates reduced-resolution forward flow and samples the future image
at those coordinates to bring it toward the current scene. All modes can use
the same region masks and feathering; alignment only affects `mode=align`.

The buffer adds up to `lookahead` seconds of video processing delay, plus one
decoded frame used to determine packet duration. Output timestamps do not move.
Live input cannot provide genuine future pixels without that buffering; the
current CLI accepts files, not live streams. A future request uses the first
available frame at or after the target time. Near EOF, or before a detected
scene cut, it clamps to the latest available frame in that segment. The final
frame therefore stays unchanged. Zero strength or zero offset passes through
without keeping a look-ahead queue.

Each inversion queue is capped at 128 frames and 128 MiB of persistent state.
Transient RGB/float masks, alignment maps and encoder buffers are additional;
the cap is not a limit on total process RSS.
High-resolution/high-rate inputs automatically shorten the effective look-ahead
and offset to fit that budget, rather than refusing to render. The nominal limit
is `(buffer capacity - 1) / fps`; requested settings are preserved. The completion
screen, plain output and JSON `warnings` report any reduction. At 2560x1440/30 fps,
the default 0.5-second request becomes about 0.367 seconds. Resize before inversion
to retain a longer horizon. A frame larger than the entire budget still requires
resizing; a one-frame capacity becomes passthrough with a warning.
An unusually dense VFR burst can fill the queue; its oldest frame then
uses the latest already-buffered future instead of exceeding the cap. Explicit
reset, rewind, frame discontinuity or a new clip clears pending state. Scene
cuts drain the old segment before starting the next, preventing future-scene
pixels from leaking backward across cuts.

The shared renderer keeps original rational video PTS through NUT RGB pipes,
including chains with sediment or collapse in either adjacent order. Unfiltered
AAC/MP3/AC3/EAC3/ALAC/Opus audio is stream-copied to avoid encoder priming shifts.
Other audio codecs or chains with audio filters are encoded to AAC; encoder
delay can apply. Temporal FFmpeg filters such as slowmo still deliberately
change timestamps. Masks/flow use seconds for analysis; exported PTS stays exact.

Tests cover early object appearance, mode differences, EOF/short/static clips,
scene boundaries, resets, bounded queues, caller ownership, VFR/nonzero-start
timestamps, copied audio packets, short pipe writes, cancellation and cleanup.
Run `python3 tests/run_tests.py inversion` for the focused suite.

`python3 tests/benchmark_inversion.py --frames 60 --output benchmarks/inversion.json`
records each mode at 640x360 and 1280x720 in fresh workers. With a 0.5-second
horizon at 30 fps, measured queue state was about 9.89 MiB at 360p and 39.55 MiB
at 720p. At 720p, processor-only averages including EOF drain were 11.93 ms
blend, 15.10 ms replace, 24.32 ms tear and 23.10 ms align. These exclude
FFmpeg decode/encode and are not end-to-end real-time claims. The JSON records
peak process RSS separately from bounded processor state.

### pixel archaeology

`archaeology` (under **TIME & MEMORY**) borrows a cutout from a *future* frame,
holds it in the present picture, then melts it back into the video. Nothing is
synthesised: every borrowed pixel is a copy of a frame that has not been shown
yet, so the picture briefly shows something that has not happened.

The mental model is a sky of drifting clouds. Somewhere in the frame a region of
cloud sits that looks like the same kind of cloud a fraction of a second later,
but the cloud has moved. Archaeology cuts that later cloud out, pastes it into
the present frame under a soft, grown mask, lets it drift with its own motion,
then dissolves it - blotch by blotch, with a flow-warped smudge - until the live
frame is back.

```sh
./effex.py archaeology input/clip.mp4
./effex.py archaeology input/clip.mp4 --set gap=30 --set size=24 --set pick=dissimilar
./effex.py archaeology input/clip.mp4 --set mode=slide --set hold=10 --set melt=24
./effex.py neon+archaeology+deepfry input/clip.mp4 --set archaeology.gap=12
./effex.py archaeology input/clip.mp4 --set seam=none --set feather=0 --set shape=rect
```

| option | range / values | default |
| --- | --- | --- |
| `lookahead` | 0.1-2.0 s of frames buffered ahead (also the output delay) | 0.6 |
| `gap` | 1-60 frames ahead the cutout is taken from | 18 |
| `cutouts` | 1-3 simultaneous cutouts | 1 |
| `spawn_every` | 4-120 frames between spawns | 24 |
| `size` | 5-40% of the frame the cutout aims to cover | 18% |
| `pick` | coherent, dissimilar, random | coherent |
| `min_motion` | 0-30%, ignore slower regions | 2% |
| `max_motion` | 1-100%, ignore faster regions | 25% |
| `similarity` | 0-100%, how close a surface must look to count as a match | 60% |
| `shape` | blob, rect | blob |
| `seam` | poisson, none | poisson |
| `feather` | 0-100%, edge softness (scaled to the blob, not the frame) | 35% |
| `hold` | 1-30 frames the cutout stays sharp | 6 |
| `melt` | 2-60 frames it takes to dissolve | 14 |
| `mode` | smudge, slide | smudge |
| `retention` | 0-95%, how much smeared detail each melt frame keeps | 70% |
| `drift` | 0-100%, how far the cutout follows its own motion | 100% |
| `seed` | 0-2147483647, 0 derives repeatable timing from the clip | 0 |

Selection is what makes or breaks the look, so it is worth knowing what the
knobs do. Every spawn tiles the frame at analysis scale (longest side <= 160 px),
rejects flat tiles, and measures two things per tile: how similar the surface is
to the same spot a `gap` frames later, and how much the pixels actually differ
there. A cutout is only eligible when the surface is similar (`similarity`) *and*
the future really differs at that spot - borrowing from a region that looks
identical ahead produces an invisible cutout, which is the old failure mode.
`coherent` then prefers the strongest visible difference inside the motion window
(`min_motion`..`max_motion`), `dissimilar` takes the least matching surface under
a cap, and `random` is the unweighted baseline.

The chosen seed tile grows into one blob by region growing on descriptor
distance, relaxing the budget in passes and finally dilating so the area tracks
`size`; morphology rounds it and a blob-scaled blur feathers the border. With
`seam=poisson` and headless OpenCV the sharp hold frames are merged with
`cv2.seamlessClone` (`NORMAL_CLONE`), otherwise a feathered alpha blend is used -
`seam=none` gives exact future pixels with a hard-ish edge, which is what the
provenance tests assert.

`mode=smudge` keeps an accumulator per cutout, `acc = warp(acc, flow) * retention
+ future * (1 - retention)`, so melting detail is warped along the region's own
motion and composited through the decaying mask; opacity eases out as the
blotches open, otherwise the tail of the melt eats the live picture. `mode=slide`
skips the accumulator and dissolves the mask with a low-frequency noise
threshold, which is cheaper and reads as a clean dissolve rather than a smear.
After `hold + melt` frames the cutout is gone and the emitted frame is the live
frame, bit-identical.

The future frames are real: the processor buffers `ceil(lookahead * fps) + 1`
frames (at most 128, and never more than the 128 MiB budget) and emits every
packet with its original index, timestamp and duration. A spawn is skipped when
no real future frame exists at the gap distance (near the end of the clip), and
the remainder is drained at EOF. Look-ahead is the only latency in the effect and
it is dropped from the output timeline, not stretched. State resets on explicit
or detected scene cuts, timestamp rewind and frame-index discontinuity, and is
bounded independently of clip length.

This effect is CPU-bound and needs NumPy (`requirements-stateful.txt`); poisson
seams and DIS optical flow need headless OpenCV (`requirements-collapse.txt`),
and without it the flow falls back to numpy block matching with a runtime note.
It has no stateful preview; it does keep original rational timestamps because it
uses the timestamped transport, so chaining with `inversion` preserves VFR input.
Benchmark the processor alone with:

```sh
python3 tests/benchmark_futurecut.py --frames 90 --output benchmarks/futurecut.json
```

Processor-only timings on this Linux x86_64 host (fresh worker per case, 90
frames, excludes FFmpeg decode/encode and is not an end-to-end claim):

| mode | 640x360 mean / p95 | 1280x720 mean / p95 | 1920x1080 mean / p95 | peak state |
| --- | --- | --- | --- | --- |
| smudge | 6.02 / 15.75 ms | 22.54 / 80.75 ms | 36.45 / 69.50 ms | 122.6 MiB |
| slide | 4.18 / 10.75 ms | 18.04 / 76.08 ms | 27.27 / 49.47 ms | 116.7 MiB |

The p95 spikes are spawn frames (region growing plus the first smudge frame).
Peak state is the buffered look-ahead plus one accumulator per live cutout, and
the buffer is what limits resolution: the 128 MiB budget fits 18 frames at
1080p and 8 at 1440p, so a long `lookahead` is clamped there with a runtime
warning instead of growing without bound. End to end, a 2560x1440 clip renders
at roughly 160 ms/frame including decode and encode.

### raster slip

`rasterslip` lives under **TIME & MEMORY**. It makes the image behave like a
display whose scan bands have lost synchronization. Some bands keep playing
live; others borrow different moments from recent history, slide sideways with
wraparound, and optionally split their RGB edges. The slipped bands travel
vertically through the frame. History stores original incoming frames, so
the effect does not accumulate an endless feedback smear.

```sh
./effex.py rasterslip input/clip.mp4
./effex.py rasterslip input/clip.mp4 --set coverage=65 --set delay=0.7 --set shift=25 --set split=2 --set seed=17
./effex.py neon+rasterslip+deepfry input/clip.mp4 --set rasterslip.speed=0.4
```

| option | meaning | default | range |
| --- | --- | --- | --- |
| `strength` | blend of delayed bands into the live picture | 85% | 0-100% |
| `coverage` | share of bands that fall out of sync, rounded up to whole bands | 40% | 0-100% |
| `band_size` | band height as a percent of the frame | 6% | 1-30% |
| `delay` | maximum requested history age, in seconds; each band has its own delay | 0.35 | 0-2 |
| `shift` | sideways tear distance as a percent of frame width | 12% | 0-50% |
| `speed` | vertical sweeps per second; 0 pins the pattern | 0.25 | 0-3 |
| `split` | RGB offset inside slipped bands, as a percent of frame width | 1% | 0-10% |
| `seed` | repeatable band positions, ages and slip directions; 0 derives from source | 0 | 0-2147483647 |

The first frame starts clean. Zero strength, coverage or delay passes frames
through the processor unchanged and keeps no frame history. With zero shift,
bands still lag in time; zero split aligns their RGB channels. Scene cuts,
rewinds, frame-index jumps, new sources and explicit resets clear the history.
Unavailable history near the start uses the earliest buffered frame.

Requires NumPy from `requirements-stateful.txt`. Persistent pixel buffers are
bounded to 128 MiB and history holds at most 64 frames; higher resolutions or
frame rates can shorten the effective delay. A reduction appears in completion
warnings and JSON. Output and temporary processing arrays are additional to
that history budget. A frame too large to fit requires resizing first.

Output keeps the frame size and source audio through the shared stateful
renderer. It chains with ordinary filters and adjacent stateful effects, such
as `neon+rasterslip+sediment+deepfry`. Standalone rendering uses the source's
nominal frame rate; there is no stateful preview.

## codec and blocks

<details>
<summary><b>crunch</b> - turn the bitrate and resolution down</summary>

| option | meaning | default | range |
| --- | --- | --- | --- |
| `res` | how far to shrink the picture | 100 | `100`, `75`, `50`, `25` |
| `bitrate` | how much data the video gets, in kbit/s | 800 | 100-5000 |

```sh
./effex.py crunch input/clip.mp4
```

</details>

### heavy datamosh

`datamosh` lives under **CODEC & BLOCKS**. Heavy is now the default: remove
recovery keyframes, crush the MPEG-4 encode, and repeatedly apply short runs of
P-frame motion/residual data to stale pixels. This is codec-level blooming,
not a pixel overlay or a repeated decoded still frame.

```sh
./effex.py datamosh input/a.mp4 --with input/b.mp4
./effex.py datamosh input/a.mp4 --with input/b.mp4 --set preset=heavy --set bloom=24 --set bloom_window=1
./effex.py datamosh input/a.mp4 --set preset=standard
./effex.py datamosh input/a.mp4 --with input/b.mp4 --set second_start=2 --set second_length=3 --set audio=mix
```

| setting | heavy (default) | standard |
| --- | --- | --- |
| `cut` | heavy: remove recovery keyframes | heavy |
| `fill` | chaos: seeded random motion frame | next motion frame |
| `hold` | 12-frame keyframe interval | 150 frames |
| `grit` | 18, visibly crushed | 2, cleaner |
| `bloom` | reuse motion across 12 frame slots | 1, disabled |
| `bloom_window` | cycle 2 captured motion frames per burst | 1 |

`bloom` ranges from 1-60 slots, and `bloom_window` from 1-12 motion frames
(capped to the burst length). Longer bursts and smaller windows sustain the
same motion for longer. Individual `--set` knobs override the chosen preset
regardless of argument order. `--set preset=...` is the **mosh** preset;
`--preset` remains the unrelated x264 encoder option.

In the menu, **second video** is the first row and **mosh preset** is the second.
Changing the preset loads its codec knobs; they remain editable afterward.
The **algorithm** row beside the preset chooses `classic` (the existing melt),
`self-mosh`, `cut shuffle`, `bloom pulses` or **Reverse Infection**. Changing a preset keeps the
selected algorithm and its timing settings.
The options scroll on short terminals. `--set second=path` still works, while
two positional files still mean two separate batch renders. Use `--with` for
one combined video.

The second clip is appended, resized/padded and frame-rate-normalized to match
the first. Its boundary keyframe is removed when compatible, so its motion
inherits the first clip's stale image instead of making a clean cut. The result
is sequential, not a simultaneous opacity blend. `audio=auto` joins audio with
silence for missing tracks; `first`, `second`, `mix` and `none` remain available.

Bloom replaces existing frame slots rather than inserting frames, preserving
the normalized timeline and audio length. Reused VOPs take the destination
timestamp; incompatible clock-header lengths keep the original slot so internal
slice markers cannot shift. A recovery keyframe can survive that safety fallback.
Very short/static inputs with little prediction data may show less melting.
The intermediate compressed stream is edited in memory, so memory use scales
with clip size. No extra packages or custom FFmpeg build are required.

Technique references: [tomato's bloom/pulse modes](https://github.com/GlitchTools/tomato),
[tiberiuiancu/datamoshing's P-frame reuse](https://github.com/tiberiuiancu/datamoshing),
and [python-moshion's two-source splice](https://github.com/rjmoggach/python-moshion).
Effex implements these techniques independently; no repository code is vendored.

#### Reverse Infection algorithm

Future fragments spread backward through consecutive optical-flow warps, growing
into the present before the actual subject arrives, then bloom forward through
moving frames in a Blockswap-style feedback pass. `bloom_strength` controls the
carried damage and `diffusion` softens its spread. Select **datamosh → algorithm
→ Reverse Infection**; its options replace the codec controls, and `p` renders a
three-second preview with buffered future context.

```sh
python -m pip install -r requirements-reverse-infection.txt
python effex.py datamosh input/clip.mp4 --set algorithm=reverse_infection --set lookahead_frames=24 --set seed=42
```

Linux, macOS and Windows use the same CPU pipeline, with original video PTS,
frame dimensions and compatible audio preserved. Set `intensity=0` to bypass.
See [parameters, implementation, measurements and tests](reverse-infection.md),
the [rendered comparison](examples/reverse-infection/comparison.mp4)
(source left, infection right), and [contact sheet](examples/reverse-infection/contact-sheet.png).

#### self-mosh algorithm

Cut one source into segments and datamosh it into itself. Each join starts a
codec melt lasting `transition` seconds, followed by a clean recovery frame.
The transition uses the incoming segment's motion against the previous image;
it does not overlap segments or shorten the timeline.

```sh
./effex.py datamosh input/clip.mp4 --set algorithm=self --set cut_length=2 --set transition=0.75
./effex.py datamosh input/clip.mp4 --set algorithm=self --set cut_length=1 --set segments=8 --set reverse=35 --set seed=17
```

| option | meaning | default | range |
| --- | --- | --- | --- |
| `algorithm` | choose the rendering method independently of the preset | classic | classic, self, shuffle, pulse, reverse_infection |
| `cut_length` | seconds per self-mosh segment | 2 | 0.1-60 |
| `segments` | 0 cuts the entire source; otherwise sample this many evenly spaced cuts | 0 | 0-128 |
| `transition` | seconds of mosh after each join; 0 gives clean cuts | 0.75 | 0-60 |
| `reverse` | independent chance each segment plays backward | 0% | 0-100% |
| `seed` | repeatable segment reversals and codec choices; 0 derives from the source | 42 | 0-2147483647 |

Automatic segments cover the whole source, including a shorter final segment.
A fixed count selects nonoverlapping cuts across the source, including its
beginning and end (a count of 1 takes the first cut). The requested cuts must fit
within the source; reduce their count or length if they do not. Lengths round to
whole frames at the source's nominal frame rate. Automatic mode supports up to
128 segments; use longer cuts or an explicit count for long sources.

Transitions stop at the end of the incoming segment. The first segment starts
clean. Heavy and standard presets still tune grit, fill and motion blooming;
`cut`, `survival` and `hold` apply to classic only. A zero transition disables
join surgery and blooming. Very short segments may have too little compatible
prediction data to melt. `--dry-run` reports the source cuts and reversals.

`audio=auto` follows the selected segments and reverses their audio with them.
`audio=first` keeps the original soundtrack, trimmed/padded to the output length;
`audio=none` removes it. Self-mosh uses a single source, so remove `--with` or
`second` before selecting it. Lossless temporary cuts use disk space in the
output folder and are removed on success, failure or cancellation. Reversal
buffers at most one cut at a time; codec surgery still holds the compressed
stream in memory.

#### cut shuffle algorithm

Choose **cut shuffle** in the algorithm row, or use `--set algorithm=shuffle`.
It selects the same cuts as self-mosh, then scrambles their playback order
before moshing the new joins. Every selected segment appears exactly once,
including a shorter final cut. With two or more cuts, the result always changes
their order. The same source, settings and seed reproduce the same cut order
and reversals.

```sh
./effex.py datamosh input/clip.mp4 --set algorithm=shuffle --set cut_length=1 --set transition=0.4 --set reverse=25 --set seed=17
```

`cut_length`, `segments`, `transition`, `reverse`, presets and audio modes work
as in self-mosh. Automatic audio travels with each shuffled cut. Use
`audio=first` to retain the original soundtrack instead. This algorithm renders
to `_SHUFFLE_MOSHED` outputs. Codec `cut=shuffle` remains the classic method
that scrambles compressed motion frames; `algorithm=shuffle` reorders the
actual source segments and melts their joins.

#### bloom pulses algorithm

Choose **bloom pulses** in the algorithm row, or use `--set algorithm=pulse`.
It keeps the source in order and repeatedly applies motion/residual data during
scheduled bursts. A clean recovery frame at the end of each burst restores
the picture, leaving clean footage between pulses. Audio plays continuously.

```sh
./effex.py datamosh input/clip.mp4 --set algorithm=pulse --set pulse_interval=2 --set pulse_length=0.5 --set pulse_start=1
```

| option | meaning | default | range |
| --- | --- | --- | --- |
| `pulse_interval` | seconds between the starts of bursts | 2 | 0.1-3600 |
| `pulse_length` | seconds per burst; 0 disables pulse surgery and blooming | 0.5 | 0-60 |
| `pulse_start` | seconds of clean footage before the first burst | 1 | 0-3600 |

Timing rounds to whole frames at the source's nominal frame rate. Each burst
must leave at least one clean frame before the next; increase `pulse_interval`
or reduce `pulse_length` if needed. Bursts are capped at the clip's end. A first
pulse past the end produces no bursts, and the first video frame always starts
clean. Up to 128 bursts are supported; increase the interval for longer clips.
`--dry-run` lists the actual pulse windows.

Presets tune grit, fill and motion bloom. Heavy repeats motion aggressively;
standard disables blooming and gives keyframe-removal bursts. Increase `bloom`
or reduce `bloom_window` to sustain repeated motion within a pulse. Very short
bursts may contain too little motion to show a strong effect. Cut depth, hold,
segment count, shuffle and reversal controls apply to other algorithms.

Use one input, with `audio=auto` / `first` to keep its soundtrack or `none` to
remove it. Outputs use `_PULSE_MOSHED`. Temporary streams are removed on success,
failure or cancellation; compressed-stream surgery still scales with clip size.

### block melt

`blockmelt` sits under **CODEC & BLOCKS**, right after `datamosh`. It fakes what
an MPEG decoder does when the residual is missing, but in pixels instead of in
the bitstream: every frame is cut into macroblocks, each block finds its motion
by block-matching the source against the previous source frame, and then the
block is copied from the previous *output* along that vector. The old picture
gets shoved around block by block - faces balloon, edges tear along the block
grid, and new content only appears where blocks leak back in.

```sh
./effex.py blockmelt input/clip.mp4
./effex.py blockmelt input/clip.mp4 --set block=32 --set repeat=3 --set leak=0
./effex.py blockmelt input/clip.mp4 --set keyframe=90 --set residual=10 --set intra=60
./effex.py blockmelt+crt input/clip.mp4 --set blockmelt.seed=17
```

| option | range | default | meaning |
| --- | --- | --- | --- |
| `block` | `8`, `16`, `32` | 16 | macroblock size in pixels |
| `search` | 4-32 px | 16 | how far each block looks for its motion |
| `residual` | 0-100% | 4 | share of the real frame difference added back; 0 is pure smear |
| `keyframe` | 0-600 frames | 0 | clean reset interval; 0 never recovers |
| `repeat` | 1-8 | 1 | motion bloom: re-apply each frame's vectors N times so motion accelerates |
| `leak` | 0-100% | 1 | chance per block per frame to refresh from the real picture |
| `intra` | 0-100% | 30 | how readily blocks with no good match are redrawn fresh, like an encoder's intra blocks; 0 never |
| `grit` | 0-100% | 40 | how blocky and posterized leaked/intra blocks come back |
| `seed` | 0-2147483647 | 0 | 0 derives a repeatable leak pattern from the clip |

Unlike `datamosh` this never touches a codec stream, so it chains like any
other stateful effect (`blockmelt+crt`, `neon+blockmelt+deepfry`) and keeps
the frame size, frame count and source audio. Motion is searched on a
downscaled luma plane, kept coherent by encoder-style biases toward zero and
neighbouring vectors, then refined to single pixels; the first frame is always
clean and scene cuts are moshed straight through, so a new shot drags the old
one. A clip that fades in from black stays dark until blocks leak or intra
refresh - raise `intra`/`leak` or set a `keyframe` interval for those. Memory
is two frames; a 720p clip renders at roughly 15-25 fps with 16 px blocks
(`block=8` is about half as fast). NumPy is required; there is no preview.

### blockswap

`blockswap` under **CODEC & BLOCKS** swaps rectangular regions of the frame
between seeded positions. The blocks do not slide: each layout holds until it
teleports.

```sh
./effex.py blockswap input/clip.mp4
./effex.py blockswap input/clip.mp4 --set blocks=5 --set size=15 --set teleport=2
./effex.py blockswap input/clip.mp4 --set teleport=1 --set mosh=25
./effex.py blockswap input/clip.mp4 --set seed=17 --set mosh=70 --set mosh_grit=20
```

| option | range | default | meaning |
| --- | --- | --- | --- |
| `size` | 5-60% | 20% | block width and height as a share of the frame |
| `blocks` | 1-8 | 3 | swapped block pairs (each pair relocates two regions) |
| `teleport` | 0-30 s | 3 | seconds per layout; 0 keeps one layout for the whole clip |
| `mosh` | 0-100% | 0 | light codec datamosh over the whole clip; 0 disables |
| `mosh_grit` | 1-31 | 12 | crunch of the intermediate mpeg4 encode |
| `mosh_hold` | 2-600 | 12 | keyframe interval in the intermediate encode |
| `seed` | 0-2147483647 | 0 | 0 derives a repeatable layout from the clip |

Each teleport window gets its own seeded layout, emitted as a `swaprect` filter
with an `enable='between(t,...)'` guard, so several layouts coexist in one
command. Windows are capped (`48 / blocks`) so a long clip cannot explode into
hundreds of filters. Blocks inside one window are kept from overlapping.

`mosh` turns blockswap into a two-pass codec render: the swaprect chain is
encoded to mpeg4 with `mosh_grit`/`mosh_hold`, recovery keyframes are removed by
survival `100 - mosh`, motion data is bloomed over `mosh/12` frame slots, and the
result is remuxed with the source audio. Teleports land in that stream as hard
prediction breaks, so the blocks themselves smear - which is the point. Higher
`mosh` keeps fewer keyframes and blooms more. Because it edits the bitstream,
blockswap cannot be chained with other effects and has no preview; with
`mosh=0` it is still encoded in a single pass (same filters, normal libx264).

### flow mosh

`flowmosh` under **CODEC & BLOCKS** is a datamosh done in pixels instead of in
the bitstream. It keeps a canvas - the moshed picture - and every frame measures
dense optical flow between the previous and current *source* frames, then drags
the canvas along that motion. Old pixels move with new motion, which is exactly
what P-frames do when their I-frame is gone, but because nothing touches the
codec it chains like any other stateful effect.

```sh
./effex.py flowmosh input/clip.mp4
./effex.py flowmosh input/clip.mp4 --set amplify=2.5 --set keyframe=0 --set bleed=0
./effex.py flowmosh input/clip.mp4 --set blocks=0 --set detail=high
./effex.py flowmosh+deepfry input/clip.mp4 --set flowmosh.refresh=scene
```

| option | range | default | meaning |
| --- | --- | --- | --- |
| `amplify` | 0-6 | 1.25 | flow multiplier; above 1 overshoots so motion smears harder, 0 freezes the canvas |
| `keyframe` | 0-600 frames | 75 | frames between canvas refreshes (seeded +-25% jitter); 0 never refreshes after the first frame |
| `bleed` | 0-100% | 2% | share of the real frame mixed back in each frame; 100 is a passthrough |
| `blocks` | 0-64 px | 12 | macroblock size: one whole-pixel motion vector per block; 0 is a smooth, liquid warp |
| `still` | 0-100% | 0 | how fast areas with no motion snap back to the live frame |
| `refresh` | `never`, `scene` | never | `scene` restarts the canvas at a detected cut; `never` lets the old shot ride the new shot's motion |
| `detail` | `low`, `medium`, `high` | medium | flow analysis size (160, 320 or 480 px on the long side) |
| `seed` | 0-2147483647 | 0 | keyframe jitter; 0 derives a repeatable seed from the clip |

Flow is OpenCV Farneback on a downscaled gray copy, measured backwards (current
to previous) so every output pixel knows where to fetch from, then upscaled and
scaled to frame pixels. In block mode each block's vector is rounded to whole
pixels and the fraction is carried to the next frame, so blocks copy crisply
instead of blurring through repeated resampling while slow motion still drifts.
Flow across a detected cut is ignored, so with `refresh=never` the old picture
holds and then starts moving with the new scene - the classic mosh transition.

State is one float canvas, two remap grids and a small gray frame (about 42 MB
at 1080p, capped at 128 MiB). The first frame is clean, and rewinds, frame-index
jumps and new sources start over. Needs NumPy and headless OpenCV
(`requirements-collapse.txt`); there is no stateful preview.

### bitrot

`bitrot` under **CODEC & BLOCKS** damages the compressed video itself. The clip
is encoded to a raw MPEG-4 Part 2 stream, then seeded bit flips, zero runs and
byte smears are written into the coded macroblock data of chosen frames. Start
codes, the VOS/VOL headers and every VOP header (frame type, clock, quantiser)
are left intact and no edit may form a new `00 00 01` start code, so the decoder
keeps every frame but loses its place inside the damaged ones: green and black
macroblock garbage, torn slices and smeared color blocks that drift through the
following motion frames until the next keyframe repairs the picture.

```sh
./effex.py bitrot input/clip.mp4
./effex.py bitrot input/clip.mp4 --set rate=30 --set burst=60
./effex.py bitrot input/clip.mp4 --set target=i --set hold=120 --set mode=zero
./effex.py bitrot input/clip.mp4 --set conceal=guess --set mode=smear --set seed=17
```

| option | range | default | meaning |
| --- | --- | --- | --- |
| `rate` | 0-100% | 15% | chance each targeted frame is damaged; 0 is a clean round trip |
| `hits` | 1-16 | 2 | corruptions written into each damaged frame |
| `mode` | `mix`, `flip`, `zero`, `smear` | `mix` | bit flips, zeroed byte runs, or runs pasted from elsewhere in the frame |
| `target` | `p`, `i`, `all` | `p` | motion frames, keyframes (bigger, longer damage) or any frame |
| `burst` | 0-100% | 30% | chance a hit keeps rotting the next 2-6 targeted frames |
| `hold` | 2-600 | 48 | keyframe interval - how long damage persists |
| `grit` | 1-31 | 8 | crunch of the intermediate mpeg4 encode; higher is blockier |
| `conceal` | `off`, `guess` | `off` | `off` shows raw broken macroblocks, `guess` lets the decoder smooth them |
| `seed` | 0-2147483647 | 0 | 0 derives a repeatable pattern from the clip |

The damaged stream is not shipped as-is. Corrupt MPEG-4 plays differently in
every decoder (some players freeze, skip or refuse it), so effex decodes it once
with `-err_detect ignore_err` and bakes the result into clean h264 with the
source audio: the output decodes without errors and looks the same everywhere.
The intermediate encode uses four slice threads so the resync layout, and the
damage for a given seed, is repeatable. bitrot runs its own pipeline, so it
cannot be chained and has no preview.
