# Standalone multi classes BYTETrack

- Standalone BYTETrack for multi-class tracking
- Independent object counting across multiple processes
- Class-specific tracking

Note: This source code is mainly based on [Supervision BYTETrack](https://github.com/roboflow/supervision)

## Installation

Currently, this package is for local use only.

1. Clone tis repository
2. Install the package in editable mode:
   ```bash
   pip install -e . --config-settings editable_mode=compat
   ```

## Usage

```python
from sbytetrack import BYTETrack

tracker = BYTETrack(n_classes=3)

xyxy_list = [[100, 100, 150, 150], [200, 200, 250, 250]]
conf_list = [0.9, 0.8]
cls_list = [0, 1]

track_ids = tracker.update(xyxy_list, conf_list, cls_list)

print(track_ids)
```
