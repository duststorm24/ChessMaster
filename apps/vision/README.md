# Chess Vision Camera App

Small Raspberry Pi 5 camera viewer for the first chess-vision milestone.

## Hardware Check

Power the Pi down before connecting the CSI ribbon cable:

```bash
ssh <pi-user>@chesspi.local
cd ~/ChessMaster/apps/vision
./shutdown_for_camera_install.sh --confirm
```

Unplug power, connect the camera to `CAM/DISP0`, reconnect power, then SSH back in.

Verify the camera:

```bash
rpicam-hello --list-cameras
rpicam-still -n -t 2s -o ~/camera-test.jpg
```

Or run the bundled diagnostic check:

```bash
cd ~/ChessMaster/apps/vision
./post_boot_camera_check.sh
```

If the camera is plugged in and still not detected, switch the Pi back to camera auto-detection and reboot:

```bash
cd ~/ChessMaster/apps/vision
./set_camera_autodetect.sh
sudo reboot
```

## Run The Web UI

On the Pi:

```bash
cd ~/ChessMaster/apps/vision
./start.sh
```

To install the web UI as a boot-time service:

```bash
cd ~/ChessMaster/apps/vision
./install_service.sh
```

From your laptop, open:

```text
http://chesspi.local:8000
```

To stop the background server:

```bash
cd ~/ChessMaster/apps/vision
./stop.sh
```

To remove the boot-time service:

```bash
cd ~/ChessMaster/apps/vision
./uninstall_service.sh
```

The UI streams the camera, includes a toggleable chessboard grid overlay, shows camera diagnostics, and saves snapshots into `captures/`. Use the calibration sliders to line the grid up with the board, or point the camera at an empty board and click `Save + detect board` to save a frame, let OpenCV estimate the grid, refresh square crops, and save an annotated board-detection debug image. Set `White at bottom` to match the camera's view. The piece-position editor lets you paint pieces onto squares, save a FEN-backed position, load the standard starting position, or clear the board. The saved-captures gallery lists snapshot images and metadata from the browser, can load saved or predicted labels back into the board painter, apply the current board-painter labels to an existing snapshot, save a labeled training sample in one step, detect the board grid, export each full-board snapshot into 64 labeled square crops under `captures/square-crops/`, and analyze those crops for brightness, contrast, and edge density. The dataset panel plus `/dataset` and `/dataset.csv` endpoints roll analyzed square crops into training-style rows with labels and features. `Train occupancy` builds a simple occupied-vs-empty baseline model, while `Train pieces` builds a starter nearest-centroid image classifier from labeled square crops. `Evaluate pieces` reports training accuracy and leave-one-out accuracy for the labeled crop dataset. `Predict pieces` writes a predicted piece map and FEN back to the capture manifest. `Recognize position` refreshes square crops, predicts pieces, and returns a confidence summary; `Save + recognize` does the same for the live camera frame. Snapshots include a JSON sidecar with the saved calibration, all 64 square rectangles, the saved piece map, and FEN so later chessboard and piece-detection code can map image pixels to squares.

The camera controls include three view modes: `HD 16:9` is a fast cropped preview, `Full FOV` uses the full sensor at `2328 x 1748`, and `Max FOV` uses the full sensor at `4656 x 3496` with a slower 1 FPS stream. Focus controls support continuous autofocus, one-shot autofocus, autofocus range/speed, and manual lens position when the connected camera exposes those controls.

Useful endpoints:

```text
/health
/diagnostics
/readiness
/captures
/dataset
/dataset.csv
/model/occupancy
/model/piece
/model/piece/evaluate
/camera-settings
/focus/trigger
/calibration
/board-map
/position
/snapshot/detect-board
/snapshot/recognize-position
/captures/<snapshot>.jpg/labels
/captures/<snapshot>.jpg/save-labeled-sample
/captures/<snapshot>.jpg/detect-board
/captures/<snapshot>.jpg/predict-pieces
/captures/<snapshot>.jpg/recognize-position
```

`/readiness` rolls camera detection, boot config, captures, calibration, dataset rows, labels, and models into one checklist. If the camera is connected but still not detected, check the boot-config gate and consider `./set_camera_autodetect.sh`.
