# -*- coding: utf-8 -*-
"""
LaserdataSkog.pyt

Skapar tre höjdraster för ett intresseområde ur Lantmäteriets Laserdata
Nedladdning, skog: ytmodell (DSM), markmodell (DTM) och höjdskillnaden mellan
dem (DSM - DTM, i praktiken vegetationshöjd).

Datakälla
---------
STAC-katalog (öppen, ingen inloggning):
    https://api.lantmateriet.se/stac-hojd/v1
    Samling: dsm-skoglig-copc
    Sök:     POST /search  {"collections": [...], "bbox": [...], "limit": 100}
             Nästa sida via länken rel="next" (method + body).

Varje item är en ruta på 10 x 10 km i SWEREF 99 TM + RH 2000 (EPSG:5845),
med en asset "data" som pekar på en COPC-fil (.copc.laz, ~1 GB):
    https://dl1.lantmateriet.se/hojd/data/pointcloud/sls/<område>/m<id>.copc.laz
proj:bbox på asset/properties ger rutans hörn i SWEREF 99 TM.

Nedladdning kräver OAuth2 (client credentials):
    POST https://apimanager.lantmateriet.se/oauth2/token
    Basic-auth med consumer key/secret, grant_type=client_credentials.
    Token gäller 3600 s. Nyckeln måste ha både API:et STAC-hojd och en
    beställning av Laserdata Nedladdning, skog, annars svarar dl1 med 403.

Hela rutor laddas aldrig ned. PDAL (ingår i ArcGIS Pro) läser COPC-filerna
direkt över HTTP och hämtar bara de delar av punktmolnet som ligger inom
intresseområdets utbredning. PDAL:s curl i Pro saknar CA-certifikat, så
ARBITER_CA_INFO pekas mot certifi innan pdal importeras - utan det fastnar
varje HTTPS-anrop i ett oändligt omförsök.

Klasser: 1 oklassad, 2 mark, 7 lågt brus, 18 högt brus. Brus tas bort före
allt annat. DSM = högsta punkt per cell, små luckor fylls från grannceller.
DTM = markpunkter trianguleras (TIN) och rastreras, alltså utan luckor.

Krav: ArcGIS Pro 3.x. arcpy, numpy, pdal och certifi ingår i arcgispro-py3.
Ingen licensnivå utöver Basic behövs.
"""

import base64
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import arcpy
import numpy as np

# ── Konstanter ────────────────────────────────────────────────────────────────

STAC_SEARCH_URL = "https://api.lantmateriet.se/stac-hojd/v1/search"
TOKEN_URL = "https://apimanager.lantmateriet.se/oauth2/token"
COLLECTION = "dsm-skoglig-copc"
GEOTORGET_URL = "https://geotorget.lantmateriet.se/geodataprodukter/laserdata-nedladdning-skog-api"

USER_AGENT = "arcgis-laserdata-skog/1.0"
HTTP_TIMEOUT = 60
HTTP_RETRIES = 3

SWEREF99TM_WKID = 3006
RH2000_WKID = 5613
WGS84_WKID = 4326

NODATA = -9999.0

CLASS_GROUND = 2
NOISE_LIMITS = "Classification![7:7],Classification![18:18]"

# Punkter läses med denna marginal runt området, så att TIN:en och DSM:ens
# lucköppning inte får sämre underlag vid kanten.
READ_MARGIN_M = 20.0

# Luckor i DSM mindre än så här många celler fylls med IDW från grannar.
DSM_WINDOW = 3

DEFAULT_CELL_SIZE = 1.0
DEFAULT_MAX_AREA_KM2 = 10.0

SUFFIX_DSM = "dsm"
SUFFIX_DTM = "dtm"
SUFFIX_DIFF = "hojdskillnad"


# =============================================================================
# HTTP
# =============================================================================

def _http(req):
    """urlopen med omförsök vid tillfälliga fel. Returnerar (status, body)."""
    delay = 2
    for attempt in range(HTTP_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            # HTTPError först: den är också URLError och OSError.
            if exc.code in (408, 429) or exc.code >= 500:
                if attempt < HTTP_RETRIES - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
            raise
        except (urllib.error.URLError, OSError):
            if attempt < HTTP_RETRIES - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise


def _get_token(key, secret):
    auth = base64.b64encode("{}:{}".format(key, secret).encode("utf-8")).decode("ascii")
    req = urllib.request.Request(
        TOKEN_URL,
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode("ascii"),
        headers={"Authorization": "Basic " + auth, "User-Agent": USER_AGENT,
                 "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        _status, body = _http(req)
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401):
            raise ValueError(
                "Lantmäteriet godkände inte consumer key/secret (HTTP {}). Kontrollera "
                "nyckeln i Lantmäteriets API-portal.".format(exc.code)
            )
        raise
    return json.loads(body.decode("utf-8"))["access_token"]


def _check_access(url, token):
    """Hämta en byte av första filen, för ett begripligt fel i stället för PDAL:s."""
    req = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + token, "Range": "bytes=0-0",
                      "User-Agent": USER_AGENT},
    )
    try:
        _http(req)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ValueError(
                "Nyckeln saknar behörighet till punktmolnen (HTTP {}). Den behöver både "
                "API:et STAC-hojd i Lantmäteriets API-portal och en beställning av "
                "Laserdata Nedladdning, skog på Geotorget:\n{}".format(exc.code, GEOTORGET_URL)
            )
        raise


def _stac_search(bbox_wgs84):
    """Alla items i samlingen som skär bbox, över alla sidor."""
    body = {"collections": [COLLECTION], "bbox": list(bbox_wgs84), "limit": 100}
    url, method = STAC_SEARCH_URL, "POST"
    items = []
    seen_pages = set()
    while url:
        data = json.dumps(body).encode("utf-8") if method == "POST" else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        _status, raw = _http(req)
        page = json.loads(raw.decode("utf-8"))
        feats = page.get("features", [])
        # Skydd mot en pager som returnerar samma sida om och om igen.
        page_key = tuple(f.get("id") for f in feats)
        if page_key in seen_pages:
            break
        seen_pages.add(page_key)
        items.extend(feats)

        nxt = next((l for l in page.get("links", []) if l.get("rel") == "next"), None)
        if not nxt or not feats:
            break
        url = nxt["href"]
        method = nxt.get("method", "GET").upper()
        body = nxt.get("body", body)
    return items


# =============================================================================
# Geometri
# =============================================================================

def _aoi_geometry(aoi_layer):
    """Alla (valda) polygoner i lagret, sammanslagna, i SWEREF 99 TM."""
    sr_in = arcpy.Describe(aoi_layer).spatialReference
    if sr_in is None or not (sr_in.factoryCode or sr_in.exportToString()):
        raise ValueError("Intresseområdet saknar koordinatsystem.")
    sr = arcpy.SpatialReference(SWEREF99TM_WKID)
    geom = None
    with arcpy.da.SearchCursor(aoi_layer, ["SHAPE@"], spatial_reference=sr) as cur:
        for (shape,) in cur:
            if shape is None or shape.area <= 0:
                continue
            geom = shape if geom is None else geom.union(shape)
    if geom is None:
        raise ValueError("Intresseområdet innehåller inga polygoner med yta.")
    return geom


def _grid(extent, cell):
    """Rutnät justerat till jämna multiplar av cellstorleken."""
    x0 = math.floor(extent.XMin / cell) * cell
    y0 = math.floor(extent.YMin / cell) * cell
    width = int(math.ceil((extent.XMax - x0) / cell))
    height = int(math.ceil((extent.YMax - y0) / cell))
    return {"resolution": cell, "origin_x": x0, "origin_y": y0,
            "width": width, "height": height}


def _pick_tiles(items, aoi):
    """
    Behåll items vars ruta skär själva polygonen (inte bara dess bbox), och bara
    den senaste insamlingen per ruta ifall Lantmäteriet har skannat om den.
    """
    sr = arcpy.SpatialReference(SWEREF99TM_WKID)
    newest = {}
    for it in items:
        asset = it.get("assets", {}).get("data")
        pb = it.get("properties", {}).get("proj:bbox") or (asset or {}).get("proj:bbox")
        if not asset or not pb:
            continue
        xmin, ymin, xmax, ymax = pb[:4]
        rect = arcpy.Polygon(arcpy.Array([
            arcpy.Point(xmin, ymin), arcpy.Point(xmin, ymax),
            arcpy.Point(xmax, ymax), arcpy.Point(xmax, ymin)]), sr)
        if aoi.disjoint(rect):
            continue
        key = tuple(round(v) for v in pb[:4])
        dt = it.get("properties", {}).get("datetime") or ""
        if key not in newest or dt > newest[key]["datetime"]:
            newest[key] = {"id": it["id"], "href": asset["href"], "datetime": dt,
                           "size": asset.get("file:size") or 0}
    return sorted(newest.values(), key=lambda t: t["id"])


# =============================================================================
# PDAL
# =============================================================================

def _import_pdal():
    # Pro:s PDAL-bygge har curl utan CA-certifikat. Utan detta misslyckas varje
    # HTTPS-anslutning och arbiter försöker om i all oändlighet.
    try:
        import certifi
        os.environ.setdefault("ARBITER_CA_INFO", certifi.where())
    except ImportError:
        pass
    try:
        import pdal
    except ImportError:
        import sys
        raise ValueError(
            "Python-paketet pdal saknas i den aktiva miljön ({}). Det ingår i "
            "standardmiljön arcgispro-py3.".format(sys.prefix)
        )
    return pdal


def _read_points(pdal, tiles, token, extent):
    bounds = "([{:.2f},{:.2f}],[{:.2f},{:.2f}])".format(
        extent.XMin - READ_MARGIN_M, extent.XMax + READ_MARGIN_M,
        extent.YMin - READ_MARGIN_M, extent.YMax + READ_MARGIN_M)
    stages = [{
        "type": "readers.copc",
        "filename": {"path": t["href"], "headers": {"Authorization": "Bearer " + token}},
        "bounds": bounds,
        "tag": "r{}".format(i),
    } for i, t in enumerate(tiles)]
    # Utan explicita inputs tar ett filter bara steget närmast före, så alla
    # läsare utom den sista skulle tyst falla bort.
    stages.append({"type": "filters.merge", "inputs": [s["tag"] for s in stages]})
    stages.append({"type": "filters.range", "limits": NOISE_LIMITS})
    pipe = pdal.Pipeline(json.dumps(stages))
    pipe.execute()
    arrays = pipe.arrays
    return arrays[0] if len(arrays) == 1 else np.concatenate(arrays)


def _write_dsm(pdal, points, grid, path):
    stage = {"type": "writers.gdal", "filename": path, "output_type": "max",
             "window_size": DSM_WINDOW, "data_type": "float32", "nodata": NODATA}
    stage.update(grid)
    pdal.Pipeline(json.dumps([stage]), arrays=[points]).execute()


def _write_dtm(pdal, points, grid, path):
    face = {"type": "filters.faceraster"}
    face.update(grid)
    stages = [
        {"type": "filters.range", "limits": "Classification[{0}:{0}]".format(CLASS_GROUND)},
        {"type": "filters.delaunay"},
        face,
        {"type": "writers.raster", "filename": path, "data_type": "float32", "nodata": NODATA},
    ]
    pdal.Pipeline(json.dumps(stages), arrays=[points]).execute()


# =============================================================================
# Utdata
# =============================================================================

def _out_path(workspace, prefix, suffix):
    name = "{}_{}".format(prefix, suffix)
    is_gdb = str(workspace).lower().endswith(".gdb")
    return os.path.join(workspace, name if is_gdb else name + ".tif")


def _default_workspace():
    try:
        gdb = arcpy.mp.ArcGISProject("CURRENT").defaultGeodatabase
        if gdb:
            return gdb
    except Exception:
        pass
    return None


def _add_to_map(paths, messages):
    try:
        aprx = arcpy.mp.ArcGISProject("CURRENT")
    except Exception:
        return
    m = aprx.activeMap
    if m is None:
        messages.addWarningMessage("Ingen aktiv karta - rastren läggs inte till.")
        return
    for p in paths:
        m.addDataFromPath(p)


# =============================================================================
# Toolbox
# =============================================================================

class Toolbox:
    def __init__(self):
        self.label = "Lantmäteriet Laserdata Skog"
        self.alias = "laserdata_skog"
        self.tools = [HojdmodellerFranLaserdata]


class HojdmodellerFranLaserdata:
    def __init__(self):
        self.label = "Höjdmodeller från Laserdata Skog"
        self.description = (
            "Skapar ytmodell (DSM), markmodell (DTM) och höjdskillnad (DSM - DTM) för "
            "ett intresseområde ur Lantmäteriets Laserdata Nedladdning, skog. Bara "
            "punkterna inom områdets utbredning hämtas, hela rutor laddas aldrig ned. "
            "Kräver en consumer key och secret från Lantmäteriets API-portal med "
            "behörighet till STAC-hojd och Laserdata Nedladdning, skog."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        p_aoi = arcpy.Parameter(
            displayName="Intresseområde", name="aoi", datatype="GPFeatureLayer",
            parameterType="Required", direction="Input",
        )
        p_aoi.filter.list = ["Polygon"]

        p_key = arcpy.Parameter(
            displayName="Consumer key", name="consumer_key", datatype="GPString",
            parameterType="Required", direction="Input", category="Inloggning",
        )
        p_secret = arcpy.Parameter(
            displayName="Consumer secret", name="consumer_secret", datatype="GPStringHidden",
            parameterType="Required", direction="Input", category="Inloggning",
        )

        p_ws = arcpy.Parameter(
            displayName="Utdata-arbetsyta", name="out_workspace", datatype="DEWorkspace",
            parameterType="Required", direction="Input",
        )
        default_ws = _default_workspace()
        if default_ws:
            p_ws.value = default_ws

        p_prefix = arcpy.Parameter(
            displayName="Namnprefix", name="prefix", datatype="GPString",
            parameterType="Required", direction="Input",
        )
        p_prefix.value = "laser"

        p_cell = arcpy.Parameter(
            displayName="Cellstorlek (m)", name="cell_size", datatype="GPDouble",
            parameterType="Optional", direction="Input", category="Avancerat",
        )
        p_cell.value = DEFAULT_CELL_SIZE

        p_max = arcpy.Parameter(
            displayName="Största tillåtna yta (km²)", name="max_area_km2", datatype="GPDouble",
            parameterType="Optional", direction="Input", category="Avancerat",
        )
        p_max.value = DEFAULT_MAX_AREA_KM2

        p_out = [
            arcpy.Parameter(displayName=label, name="out_" + suffix, datatype="DERasterDataset",
                            parameterType="Derived", direction="Output")
            for label, suffix in (("DSM", SUFFIX_DSM), ("DTM", SUFFIX_DTM),
                                  ("Höjdskillnad", SUFFIX_DIFF))
        ]

        return [p_aoi, p_key, p_secret, p_ws, p_prefix, p_cell, p_max] + p_out

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        return

    def updateMessages(self, parameters):
        p_ws, p_prefix, p_cell, p_max = parameters[3:7]

        prefix = (p_prefix.valueAsText or "").strip()
        if prefix and not (prefix[0].isalpha() and all(c.isalnum() or c == "_" for c in prefix)):
            p_prefix.setErrorMessage(
                "Prefixet får bara innehålla bokstäver, siffror och understreck, och "
                "måste börja med en bokstav."
            )
        elif prefix and p_ws.valueAsText:
            existing = [_out_path(p_ws.valueAsText, prefix, s)
                        for s in (SUFFIX_DSM, SUFFIX_DTM, SUFFIX_DIFF)]
            existing = [os.path.basename(p) for p in existing if arcpy.Exists(p)]
            if existing:
                p_prefix.setWarningMessage("Skrivs över: " + ", ".join(existing))

        if p_cell.value is not None and not (0.25 <= p_cell.value <= 50):
            p_cell.setErrorMessage("Cellstorleken ska vara mellan 0,25 och 50 m.")
        elif p_cell.value is not None and p_cell.value < 1:
            p_cell.setWarningMessage(
                "Punkttätheten är 1-2 punkter/m². Under 1 m blir DSM:en glest fylld och "
                "DTM:en bara interpolerad mellan markpunkterna."
            )

        if p_max.value is not None and p_max.value <= 0:
            p_max.setErrorMessage("Ange en yta större än 0.")

    def execute(self, parameters, messages):
        aoi = parameters[0].value
        key = (parameters[1].valueAsText or "").strip()
        secret = (parameters[2].valueAsText or "").strip()
        workspace = parameters[3].valueAsText
        prefix = parameters[4].valueAsText.strip()
        cell = parameters[5].value or DEFAULT_CELL_SIZE
        max_area = parameters[6].value or DEFAULT_MAX_AREA_KM2

        try:
            outputs = _run(aoi, key, secret, workspace, prefix, cell, max_area, messages)
        except ValueError as exc:
            messages.addErrorMessage(str(exc))
            raise arcpy.ExecuteError

        for i, path in enumerate(outputs):
            arcpy.SetParameterAsText(7 + i, path)

    def postExecute(self, parameters):
        return


# =============================================================================
# Körningens innehåll (separat funktion - går att testa utanför Pro)
# =============================================================================

def _run(aoi_layer, key, secret, workspace, prefix, cell, max_area_km2, messages):
    aoi = _aoi_geometry(aoi_layer)
    ext = aoi.extent
    area_km2 = (ext.XMax - ext.XMin) * (ext.YMax - ext.YMin) / 1e6
    messages.addMessage("Intresseområdets utbredning: {:.2f} km² (SWEREF 99 TM).".format(area_km2))
    if area_km2 > max_area_km2:
        raise ValueError(
            "Utbredningen är {:.1f} km², mer än tillåtna {:.1f} km². Alla punkter hålls i "
            "minnet (ungefär 300 MB per km²), så dela upp området eller höj gränsen under "
            "Avancerat.".format(area_km2, max_area_km2)
        )

    wgs = aoi.projectAs(arcpy.SpatialReference(WGS84_WKID)).extent
    arcpy.SetProgressor("default", "Söker rutor i Lantmäteriets STAC-katalog...")
    try:
        items = _stac_search((wgs.XMin, wgs.YMin, wgs.XMax, wgs.YMax))
        tiles = _pick_tiles(items, aoi)
        if not tiles:
            raise ValueError(
                "Inga laserdata för området. Laserdata Skog täcker ungefär 75 % av Sverige, "
                "men inte fjällen."
            )
        years = sorted({t["datetime"][:4] for t in tiles if t["datetime"]})
        messages.addMessage("{} ruta/rutor: {} (insamlingsår {}).".format(
            len(tiles), ", ".join(t["id"] for t in tiles), ", ".join(years) or "okänt"))
        if len(years) > 1:
            messages.addWarningMessage(
                "Rutorna är skannade olika år. Höjdskillnaden kan ha en skarv vid rutgränsen."
            )

        arcpy.SetProgressorLabel("Hämtar token...")
        token = _get_token(key, secret)
        _check_access(tiles[0]["href"], token)

        pdal = _import_pdal()
        arcpy.SetProgressorLabel("Läser punkter inom området...")
        t0 = time.time()
        points = _read_points(pdal, tiles, token, ext)
        messages.addMessage("Läste {:,} punkter på {:.0f} s.".format(len(points), time.time() - t0)
                            .replace(",", " "))
        if len(points) == 0:
            raise ValueError("Inga punkter inom området.")

        grid = _grid(ext, cell)
        scratch = arcpy.env.scratchFolder
        tmp_dsm = os.path.join(scratch, "lds_dsm.tif").replace("\\", "/")
        tmp_dtm = os.path.join(scratch, "lds_dtm.tif").replace("\\", "/")
        tmp_diff = os.path.join(scratch, "lds_diff.tif")

        arcpy.SetProgressorLabel("Skapar DSM...")
        _write_dsm(pdal, points, grid, tmp_dsm)
        arcpy.SetProgressorLabel("Skapar DTM...")
        _write_dtm(pdal, points, grid, tmp_dtm)
        del points

        arcpy.SetProgressorLabel("Beräknar höjdskillnad...")
        dsm = arcpy.RasterToNumPyArray(tmp_dsm, nodata_to_value=np.nan)
        dtm = arcpy.RasterToNumPyArray(tmp_dtm, nodata_to_value=np.nan)
        diff = dsm - dtm
        # Små negativa värden är mätbrus (DSM:ens högsta punkt under TIN:en).
        diff = np.where(diff < 0, 0, diff)
        diff = np.where(np.isnan(diff), NODATA, diff).astype(np.float32)

        sr = arcpy.SpatialReference(SWEREF99TM_WKID, RH2000_WKID)
        old_ocs = arcpy.env.outputCoordinateSystem
        old_overwrite = arcpy.env.overwriteOutput
        try:
            arcpy.env.outputCoordinateSystem = sr
            arcpy.env.overwriteOutput = True
            arcpy.NumPyArrayToRaster(
                diff, arcpy.Point(grid["origin_x"], grid["origin_y"]), cell, cell, NODATA
            ).save(tmp_diff)

            clip_fc = arcpy.management.CopyFeatures([aoi], r"memory\lds_aoi")[0]
            rect = "{} {} {} {}".format(ext.XMin, ext.YMin, ext.XMax, ext.YMax)
            outputs = []
            for src, suffix in ((tmp_dsm, SUFFIX_DSM), (tmp_dtm, SUFFIX_DTM),
                                (tmp_diff, SUFFIX_DIFF)):
                arcpy.SetProgressorLabel("Klipper {}...".format(suffix))
                dst = _out_path(workspace, prefix, suffix)
                arcpy.management.Clip(src, rect, dst, clip_fc, str(NODATA),
                                      "ClippingGeometry", "NO_MAINTAIN_EXTENT")
                # Clip tappar PDAL:s sammansatta koordinatsystem, sätt det igen.
                arcpy.management.DefineProjection(dst, sr)
                outputs.append(dst)
                messages.addMessage("Skapade {}.".format(dst))
            arcpy.management.Delete(clip_fc)
        finally:
            arcpy.env.outputCoordinateSystem = old_ocs
            arcpy.env.overwriteOutput = old_overwrite

        for p in (tmp_dsm, tmp_dtm, tmp_diff):
            try:
                arcpy.management.Delete(p)
            except Exception:
                pass

        _add_to_map(outputs, messages)
        return outputs
    finally:
        arcpy.ResetProgressor()
