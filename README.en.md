# A/V sync test pattern — Bobi.Studio's lip-sync instrument

*[Version française](README.md)*

Measures the picture/sound offset introduced by a chain. Submodule of
[bobi.studio](https://github.com/bob-integration/bobistudio), mounted at `plugins/avsync/`.

## What it does

A full-screen flash and a beep, **locked to the same clock**, plus a `sec.frame` counter, a sweep
bar and a graphics overlay. Three outputs on the MXL bus: video (`<host>`), 8-channel audio
(`<host>_audio`), and a timecode ANC flow (`<host>_anc`) when the generator is enabled.

Wire the pattern into the input of whatever you want to measure, and read the gap between flash
and beep at the output: that is the offset of everything in between. To **isolate one stage**,
place one pattern before it and one after, and subtract.

## Audio mark

Two sound forms to choose from. The **beep**, a 1 kHz sine identical on every beat, measures an
offset of less than half a beat; beyond that, the measurement is ambiguous by a whole period.

The **mark** dates each beat: 20 ms of a 1→4 kHz chirp for anchoring, then two 20 ms pure tones
carrying a 6-bit counter — 64 beats before it repeats. It is the form to use behind a codec: the
chirp gives a sharp correlation peak where the edge of a sine gets smeared, and pure tones are
what AAC and Opus preserve best.

## Probe bands

Two bands of 32 binary blocks in luma, carrying **per band** the grain index and the band number,
with an XOR check. The per-band index gives the real age of the picture, and two bands with
different indices point to a torn frame — something a single timecode in the middle of the
picture cannot tell apart. Two layouts: spread over the height, the only one that says **which**
band falls behind; at both edges, it keeps the picture watchable. The blocks are still 3.7 px wide
at 1/16 scale. Off by default, zero cost.

## Timecode

When enabled, the generator burns in the time of day in place of the counter and publishes the
**same** timecode as ANC (ATC RP 188 / RFC 8331), which can be wired to a multiviewer window, for
instance. The time comes from the node's clock, on the PTP timescale, shown in the control room's
time zone. Under genlock, the timecode is computed on the frame's presentation grid, so it is
exactly aligned with the picture.

Without working PTP on the node, the time drifts: the node's PTP state is the first thing to check
when a timecode looks wrong.

## Genlock and cadence

On by default: flash, beep and cadence are locked to the PTP grid. When off, the pattern runs
free — the lip-sync measurement remains valid, but the pattern is no longer comparable with other
genlocked flows.

Audio leaves in packets whose size comes from the core setting (Settings → MXL, 1 ms by
default); the plugin reads it, it does not decide it.

## Slice mode

The frame can be published band by band. Since rendering the pattern is near-instant, its own
gain is small here: the point is to keep the sub-frame path intact end to end on a chain that
relies on it elsewhere. Audio and flash/beep timing are strictly unchanged.

## Diagnostics

The `:8080` metrics publish `fps`, `frame_index` and `own_latency_ms` — the rendering's own cost,
excluding the cadence wait, to check that the generator itself does not drift. `GET :8082/state`
returns the complete effective configuration. The log level can be changed live.

## Licence

GNU GPL v3 or later — see `LICENSE`.
