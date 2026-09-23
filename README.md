# Laserdata Skog

ArcGIS Pro Python toolbox that builds three height rasters for an area of interest from
Lantmäteriet's Laserdata Nedladdning, skog:

- **DSM** (ytmodell): highest point per cell.
- **DTM** (markmodell): ground points triangulated and rasterised, so it has no gaps.
- **Höjdskillnad**: DSM minus DTM, in practice vegetation and building height. Negative values
  are set to 0.

Only the points inside the area's bounding box are read, straight from Lantmäteriet's
cloud-optimised files. Whole 10 x 10 km tiles (about 1 GB each) are never downloaded, and ArcGIS
never has to open the point cloud.

## Requirements

- ArcGIS Pro 3.x, Basic licence is enough. Developed and tested on 3.6 with Python 3.13.
- No extra packages. `pdal`, `numpy` and `certifi` ship with the default `arcgispro-py3`
  environment.
- A consumer key and secret from Lantmäteriet's API portal with **both**:
  - the API `STAC-hojd`, and
  - an order of [Laserdata Nedladdning, skog](https://geotorget.lantmateriet.se/geodataprodukter/laserdata-nedladdning-skog-api)
    on Geotorget (Beställning tab).

  With only the first, the tool fails with HTTP 403 and says so.

## Install

1. Clone or download this repo.
2. In ArcGIS Pro: Catalog, Toolboxes, Add Toolbox, select `LaserdataSkog.pyt`.
3. Open Lantmäteriet Laserdata Skog, Höjdmodeller från Laserdata Skog.

## Parameters

| Parameter | Default | Notes |
|---|---|---|
| Intresseområde | - | Polygon layer in any coordinate system. All features, or the selection, are merged |
| Consumer key / Consumer secret | - | Under Inloggning. The secret is a hidden field |
| Utdata-arbetsyta | project geodatabase | Geodatabase or folder. In a folder the rasters are GeoTIFF |
| Namnprefix | `laser` | Outputs are `<prefix>_dsm`, `<prefix>_dtm`, `<prefix>_hojdskillnad`. Existing ones are overwritten, with a warning in the dialog |
| Cellstorlek (m) | 1 | Under Avancerat |
| Största tillåtna yta (km²) | 10 | Under Avancerat. All points are held in memory, about 300 MB per km² of bounding box |

The rasters are clipped to the polygon, snapped to whole multiples of the cell size, in
SWEREF 99 TM + RH 2000, and added to the active map.

## About the data

- Density is 1-2 points per m², with roughly one ground point per m² in open forest. 1 m cells
  are a good default. Finer cells leave the DSM sparse.
- Classes used: 2 (mark) for the DTM, everything except 7 and 18 (noise) for the DSM.
- The DSM is NoData where no laser returns exist, typically open water. The DTM interpolates
  across such areas.
- If Lantmäteriet has scanned a tile more than once, the newest scan is used. When an area spans
  tiles from different years, the tool warns that the height difference may show a seam.
- Coverage is about 75 % of Sweden. The mountains are not included.
