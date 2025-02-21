import os
import whitebox
import geopandas as gpd
import rasterio
from rasterio.warp import calculate_default_transform, reproject, Resampling
from flask import Flask, request, render_template, send_file, redirect, url_for, jsonify
import zipfile
import time
import threading

app = Flask(__name__)
UPLOAD_FOLDER = "uploads"
RESULT_FOLDER = r"I:\Flask_App\outputs"  # Updated output directory
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(RESULT_FOLDER, exist_ok=True)  # Ensure the outputs directory exists

wbt = whitebox.WhiteboxTools()
wbt.set_working_dir(RESULT_FOLDER)  # Set working directory to outputs

# Global variable to track progress
progress = 0

def get_epsg_code(projection):
    if "WGS 1984 UTM Zone" in projection:
        zone = int(projection.split(" ")[-1][:-1])
        return f"EPSG:{32600 + zone}" if "N" in projection else f"EPSG:{32700 + zone}"
    elif "NAD83 / UTM zone" in projection:
        zone = int(projection.split(" ")[-2][:-1])
        return f"EPSG:{26900 + zone}"
    elif "NAD27 / UTM zone" in projection:
        zone = int(projection.split(" ")[-2][:-1])
        return f"EPSG:{26700 + zone}"
    elif "ETRS89 / UTM zone" in projection:
        zone = int(projection.split(" ")[-2][:-1])
        return f"EPSG:{25800 + zone}"
    return "EPSG:4326"  # Default to WGS84

def create_zip_for_shapefile(shapefile_path):
    """Create a zip file containing all supporting files for a shapefile."""
    base_name = os.path.splitext(shapefile_path)[0]
    zip_path = f"{base_name}.zip"
    with zipfile.ZipFile(zip_path, 'w') as zipf:
        for ext in ['.shp', '.shx', '.dbf', '.prj', '.cpg']:
            file_path = f"{base_name}{ext}"
            if os.path.exists(file_path):
                zipf.write(file_path, os.path.basename(file_path))
    return zip_path

@app.route('/')
def home():
    return render_template('upload.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    global progress
    progress = 0  # Reset progress

    if 'file' not in request.files:
        return "No file uploaded", 400
    file = request.files['file']
    if file.filename == '':
        return "No selected file", 400
    
    projection = request.form.get("projection", "WGS 1984 UTM Zone 44N")
    epsg_code = get_epsg_code(projection)
    threshold = int(request.form.get("threshold", 1000))
    
    input_dem = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(input_dem)
    
    # Define output file paths in the outputs directory with generic names
    dem_utm = os.path.join(RESULT_FOLDER, "DEM.tif")
    filled_dem = os.path.join(RESULT_FOLDER, "Filled_DEM.tif")
    flow_direction = os.path.join(RESULT_FOLDER, "Flow_Direction.tif")
    flow_accumulation = os.path.join(RESULT_FOLDER, "Flow_Accumulation.tif")
    streams = os.path.join(RESULT_FOLDER, "Streams.tif")
    strahler_streams = os.path.join(RESULT_FOLDER, "Strahler_Streams.tif")
    streams_undissolved = os.path.join(RESULT_FOLDER, "Streams_Undissolved.shp")
    streams_dissolved = os.path.join(RESULT_FOLDER, "Streams_Dissolved.shp")

    # Start processing in a separate thread
    threading.Thread(target=process_file, args=(input_dem, epsg_code, threshold, dem_utm, filled_dem, flow_direction, flow_accumulation, streams, strahler_streams, streams_undissolved, streams_dissolved)).start()

    return jsonify({"status": "Processing started"})

def process_file(input_dem, epsg_code, threshold, dem_utm, filled_dem, flow_direction, flow_accumulation, streams, strahler_streams, streams_undissolved, streams_dissolved):
    global progress

    # Reproject DEM
    with rasterio.open(input_dem) as src:
        if src.crs is None:
            progress = 100
            return
        
        transform, width, height = calculate_default_transform(
            src.crs, epsg_code, src.width, src.height, *src.bounds
        )
        kwargs = src.meta.copy()
        kwargs.update({"crs": epsg_code, "transform": transform, "width": width, "height": height})
        
        with rasterio.open(dem_utm, "w", **kwargs) as dst:
            for i in range(1, src.count + 1):
                reproject(
                    source=rasterio.band(src, i),
                    destination=rasterio.band(dst, i),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=transform,
                    dst_crs=epsg_code,
                    resampling=Resampling.nearest
                )
    progress = 10  # 10% complete

    # Hydrology Analysis
    wbt.fill_depressions_wang_and_liu(dem_utm, filled_dem, fix_flats=True)
    progress = 20  # 20% complete

    wbt.d8_pointer(filled_dem, flow_direction, esri_pntr=False)
    progress = 30  # 30% complete

    wbt.d8_flow_accumulation(filled_dem, flow_accumulation, out_type="cells")
    progress = 40  # 40% complete

    wbt.extract_streams(flow_accumulation, streams, threshold)
    progress = 50  # 50% complete

    wbt.strahler_stream_order(flow_direction, streams, strahler_streams)
    progress = 60  # 60% complete

    # Check if streams and flow_direction exist before converting to vector
    if not os.path.exists(streams) or not os.path.exists(flow_direction):
        progress = 100
        return
    
    wbt.raster_streams_to_vector(strahler_streams, flow_direction, streams_undissolved, esri_pntr=False)
    progress = 70  # 70% complete

    # Check if streams_undissolved exists before loading
    if not os.path.exists(streams_undissolved):
        progress = 100
        return
    
    # Load Vector Data
    gdf = gpd.read_file(streams_undissolved)
    progress = 80  # 80% complete

    # Assign Projection if Missing
    if gdf.crs is None:
        gdf.set_crs(epsg_code, inplace=True)
        gdf.to_file(streams_undissolved, driver="ESRI Shapefile")
    progress = 90  # 90% complete

    # Dissolve Stream Network
    if "STRM_VAL" in gdf.columns and not gdf["STRM_VAL"].isnull().all():
        gdf_dissolved = gdf.dissolve(by="STRM_VAL")
        gdf_dissolved.to_file(streams_dissolved, driver="ESRI Shapefile")
    progress = 100  # 100% complete

@app.route('/progress')
def get_progress():
    global progress
    return jsonify({"progress": progress})

@app.route('/result')
def result():
    # Prepare Download Links
    streams_undissolved_zip = create_zip_for_shapefile(os.path.join(RESULT_FOLDER, "Streams_Undissolved.shp"))
    streams_dissolved_zip = create_zip_for_shapefile(os.path.join(RESULT_FOLDER, "Streams_Dissolved.shp"))
    
    download_links = {
        "Reprojected DEM": url_for('download_file', filename="DEM.tif"),
        "Filled DEM": url_for('download_file', filename="Filled_DEM.tif"),
        "Flow Direction": url_for('download_file', filename="Flow_Direction.tif"),
        "Flow Accumulation": url_for('download_file', filename="Flow_Accumulation.tif"),
        "Streams": url_for('download_file', filename="Streams.tif"),
        "Strahler Streams": url_for('download_file', filename="Strahler_Streams.tif"),
        "Streams (Undissolved)": url_for('download_file', filename=os.path.basename(streams_undissolved_zip)),
        "Streams (Dissolved)": url_for('download_file', filename=os.path.basename(streams_dissolved_zip))
    }
    
    
    return render_template('result.html', download_links=download_links)

@app.route('/download/<filename>')
def download_file(filename):
    return send_file(os.path.join(RESULT_FOLDER, filename), as_attachment=True)

if __name__ == '__main__':
    app.run(debug=True)