# ONVIF Settings File (TOML)

Use a `.toml` file with a required `name` and `[[steps]]` entries. Each step applies PTZ and/or focus settings, waits for `delay_sec`, then saves a snapshot named `TIMESTAMP_stepname.jpg` under a subfolder named after `name`.

Example:

```toml
name = "calibration_run"

[[steps]]
name = "wide_auto"
delay_sec = 1
focus_mode = "AUTO"
ptz.type = "absolute"
ptz.pan = 0.0
ptz.tilt = 0.0
ptz.zoom = 0.0

[[steps]]
name = "zoom_in"
delay_sec = 1
ptz.type = "relative"
ptz.zoom = 0.2
ptz.speed_zoom = 0.5

[[steps]]
name = "manual_focus"
delay_sec = 1
focus_mode = "MANUAL"
focus_near_limit = 200.0
focus_default_speed = 0.5
```

Supported keys:

- `name` (string, required): used to create the subfolder under the save directory.
- `steps[].name` (string, optional): used in filename.
- `delay_sec` (number, optional): wait before snapshot.
- `ptz.type` (string, optional): `relative`, `absolute`, or `continuous`.
- `ptz.pan`, `ptz.tilt`, `ptz.zoom` (number, optional): position/translation/velocity depending on `ptz.type`.
- `ptz.speed_pan`, `ptz.speed_tilt`, `ptz.speed_zoom` (number, optional): speeds for `relative`/`absolute`.
- `ptz.duration_sec` (number, optional): duration for `continuous` before stop.
- `focus_mode` (string, optional): `AUTO` or `MANUAL`.
- `focus_default_speed` (number, optional).
- `focus_near_limit` (number, optional).
- `focus_far_limit` (number, optional).

PTZ ranges (from camera `192.168.254.3` test):

- Pan: `-1.0 .. 1.0`
- Tilt: `-1.0 .. 1.0`
- Zoom: `0.0 .. 1.0`
- Pan/Tilt speed: `0.0 .. 1.0`
- Zoom speed: `0.0 .. 1.0`
- Continuous velocity: `-1.0 .. 1.0` (Pan/Tilt), `-1.0 .. 1.0` (Zoom)

Focus ranges (from `192.168.254.3` imaging settings):

- `AutoFocusMode`: `AUTO` or `MANUAL`
- `NearLimit`: `0 .. 300`
- `DefaultSpeed`: camera-dependent (example returned `None` unless set)

To verify ranges on another camera, query ONVIF PTZ nodes and imaging settings.
