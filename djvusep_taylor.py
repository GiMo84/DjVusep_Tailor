import os
import subprocess
import logging
import click
import click_logging
import tempfile
from PIL import Image
import concurrent.futures
from tqdm import tqdm
from functools import partial
import shutil # Needed for file copying

from pdfrw import PdfWriter, PdfReader, PageMerge, IndirectPdfDict, PdfDict, PdfArray, PdfString, PdfName

logger = logging.getLogger(__name__)
click_logging.basic_config(logger)


class CommandError(Exception):
    """
    Exception raised when a command fails to execute correctly.

    Attributes:
        message (str): The error message.
        stderr (str): The standard error output of the command.
    """

    def __init__(self, message, stderr):
        super().__init__(message)
        self.stderr = stderr


def run_command(command, input_data=None):
    """
    Executes a command and captures its output.

    Args:
        command (list): The command to execute as a list of strings.
        input_data (bytes, optional): The input data to pass to the command's standard input. Defaults to None.

    Returns:
        tuple: A tuple containing the command's standard output, standard error, and the process object.

    Raises:
        CommandError: If the command fails to execute correctly.

    """
    try:
        logger.debug(f"Running command: {' '.join(command)}")
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
        )
        stdout, stderr = process.communicate(input_data)
        returncode = process.returncode
        logger.debug(f"{os.path.basename(command[0])} [{str(returncode)}]: {stderr.decode(errors='ignore')}") # Decode stderr safely
        if len(stdout) > 0:
            logger.debug(f"{os.path.basename(command[0])} returned {len(stdout)} bytes.")
        if returncode != 0:
            raise CommandError("Command {} failed with return code {}: {}".format(command, returncode), stderr.decode(errors='ignore'))
        return stdout, stderr, process
    except Exception as e:
        raise CommandError("Error running command {}: {}".format(command, str(e)), "")

# --- PDF-specific helper functions ---

def create_jbig2_global_dict_and_pages(temp_dir, all_fg_tiffs, jbig2_output_basename, jbig2_losslevel):
    """
    Creates a global JBIG2 dictionary and then individual JBIG2 segments
    for each foreground image, leveraging the global dictionary.

    Args:
        temp_dir (str): Temporary directory for intermediate files.
        all_fg_tiffs (list): List of paths to 1-bit foreground TIFFs for all pages.
        global_dict_path (str): Path where the global JBIG2 dictionary (.sym) will be saved.
        jbig2_losslevel (int): JBIG2 loss level (0 for lossless).

    Returns:
        dict: A dictionary mapping input TIFF paths to their generated JBIG2 (.jb2) paths.
    """
    logger.info("Phase 1: Creating global JBIG2 dictionary for all foregrounds...")
    
    # Step 1a: Merge all foreground TIFFs into a single multi-page TIFF
    if not all_fg_tiffs:
        logger.warning("No foreground TIFFs provided for global JBIG2 dictionary. Skipping dictionary creation.")
        return {}

    multi_page_tiff_path = os.path.join(temp_dir, "all_foregrounds_merged.tif")
    
    # Open the first image
    images_to_merge = [Image.open(f) for f in all_fg_tiffs]
    
    # Save as multi-page TIFF
    if images_to_merge:
        logger.debug(f"Merging {len(images_to_merge)} foreground TIFFs into {multi_page_tiff_path}...")
        images_to_merge[0].save(
            multi_page_tiff_path,
            save_all=True,
            append_images=images_to_merge[1:],
            compression="tiff_ccittfax4", # Common for 1-bit TIFF
            dpi=(300, 300) # Assuming input resolution
        )
        logger.debug("Multi-page TIFF created.")
    else:
        logger.warning("No images to merge for global dictionary.")
        return {}


    # Step 2: Run jbig2enc on the multi-page TIFF
    # jbig2enc with -s (symbols/dictionary) generates <output_base>.sym and <output_base>.0000, etc.
    # The output files will be created directly in `temp_dir` because the command is run from there or with full paths.
    # We specify the output base name with -o (output filename prefix).
    jbig2_output_prefix = os.path.join(temp_dir, jbig2_output_basename)
    jbig2_cmd = ["jbig2", "-s", "-b", jbig2_output_prefix, "-p", multi_page_tiff_path]
    if jbig2_losslevel > 0:
        jbig2_cmd.extend(["-l", str(jbig2_losslevel)]) # Add loss level
    
    global_dict_file_path = f"{jbig2_output_prefix}.sym" # Expected global dictionary file

    try:
        run_command(jbig2_cmd)
        logger.info(f"JBIG2 dictionary and page segments generated with base: {jbig2_output_prefix}")
    except CommandError as e:
        logger.error(f"Error running jbig2enc for global dictionary: {e.stderr}")
        return {}
    finally:
        # Clean up the merged multi-page TIFF
        if os.path.exists(multi_page_tiff_path):
            os.remove(multi_page_tiff_path)
            logger.debug(f"Removed temporary multi-page TIFF: {multi_page_tiff_path}")

    # Step 3: Collect the generated page-specific JBIG2 files
    jbig2_paths = {}
    
    # jbig2enc outputs files named <output_base>.<page_number_zero_padded>
    # We need to map these back to the original input TIFF paths (all_fg_tiffs)
    # based on their order.
    for i, original_fg_tiff_path in enumerate(all_fg_tiffs):
        # Determine the expected output JBIG2 file name for this page
        generated_jb2_path = f"{jbig2_output_prefix}.{i:04d}" # e.g., /tmp/jbig2_output.0000
        
        if os.path.exists(generated_jb2_path):
            jbig2_paths[original_fg_tiff_path] = generated_jb2_path
        else:
            logger.warning(f"Expected JBIG2 file {generated_jb2_path} not found for original {original_fg_tiff_path}. This page might be missing JBIG2 data.")
            # Decide if this is a critical error or if you can continue with some pages missing JBIG2

    return jbig2_paths, global_dict_file_path

def create_jpeg_from_image(input_path, output_jpeg_path, quality):
    """
    Compresses an image to JPEG.
    """
    logger.debug(f"Compressing {input_path} to JPEG with quality {quality}...")
    try:
        img = Image.open(input_path)
        if img.mode != 'RGB':
            img = img.convert('RGB')
        img.save(output_jpeg_path, 'jpeg', quality=quality)
        logger.debug(f"JPEG created at: {output_jpeg_path}")
    except Exception as e:
        logger.error(f"Error creating JPEG from {input_path}: {e}")
        raise

def process_page_for_pdf(
    page_identifier, # filename or page_name for progress tracking
    inputdir,
    background_dir,
    foreground_dir,
    temp_dir,
    jpeg_quality,
):
    """
    Prepares image layers for a single PDF page.
    This function extracts/processes images and returns paths to processed layers
    along with page dimensions.
    It does NOT perform JBIG2 compression here, that's done globally later.

    Returns:
        tuple: (original_filename, dict_of_paths_to_layers, page_dimensions_in_pixels)
        dict_of_paths_to_layers keys: 'background_jpeg', 'foreground_tiff_1bit', 'mask_tiff_1bit'
    """
    file_path = os.path.join(inputdir, page_identifier)
    page_name = os.path.splitext(page_identifier)[0]
    
    # Store paths to temporary files that will be generated for this page
    page_temp_paths = {
        'background_jpeg': os.path.join(temp_dir, f"page_{page_name}_bg.jpeg"),
        'foreground_tiff_1bit': os.path.join(temp_dir, f"page_{page_name}_fg_1bit.tif"),
        'mask_tiff_1bit': os.path.join(temp_dir, f"page_{page_name}_mask_1bit.tif")
    }

    page_width, page_height = None, None

    background_input_path = os.path.join(background_dir, f"{page_name}.tif")
    foreground_input_path = os.path.join(foreground_dir, f"{page_name}.tif")

    if os.path.exists(background_input_path) and os.path.exists(foreground_input_path):
        # Case 1: Separate foreground and background
        logger.debug(f"Processing page {page_name} as layered (background/foreground).")
        
        # Extract background (color) and compress to JPEG
        temp_bg_tiff = os.path.join(temp_dir, f"page_{page_name}_bg_raw.tif")
        # Assuming background_input_path is a full image (e.g., color TIFF)
        # We don't need ddjvu here if input is already a TIFF. We can directly open with PIL.
        # However, if it's a DjVu page, then ddjvu is needed. Let's assume inputs are TIFFs based on context.
        create_jpeg_from_image(background_input_path, page_temp_paths['background_jpeg'], jpeg_quality)

        # Extract foreground (black & white for JBIG2)
        # Similar assumption: foreground_input_path is a 1-bit TIFF or can be converted to one.
        with Image.open(foreground_input_path) as fg_img:
            if fg_img.mode != '1':
                fg_img = fg_img.convert('1')
            fg_img.save(page_temp_paths['foreground_tiff_1bit'])

        # Create mask: For this case, the 1-bit foreground *is* the mask for its own transparent layer.
        shutil.copy(page_temp_paths['foreground_tiff_1bit'], page_temp_paths['mask_tiff_1bit'])

        # Determine page dimensions from background image
        with Image.open(page_temp_paths['background_jpeg']) as img:
            page_width, page_height = img.width, img.height

    else:
        # Case 2: Single input image
        logger.debug(f"Processing page {page_name} as single image.")
        img = Image.open(file_path)
        
        # Determine page dimensions from the input image itself
        page_width, page_height = img.width, img.height

        if img.mode == "1":
            # Bitonal page: This becomes the foreground and its own mask
            # Copy input directly to foreground and mask TIFFs (already 1-bit)
            shutil.copy(file_path, page_temp_paths['foreground_tiff_1bit'])
            shutil.copy(file_path, page_temp_paths['mask_tiff_1bit'])
            
            # Create a white JPEG to act as the background (will be largely covered by opaque foreground)
            dummy_bg_img = Image.new('RGB', (page_width, page_height), (255, 255, 255))
            dummy_bg_img.save(page_temp_paths['background_jpeg'], 'jpeg', quality=jpeg_quality)

        elif img.mode in ('RGB', 'L', 'P'): # Include Palette mode for general images
            # Photo page: This becomes the background, with an "empty" foreground/mask
            create_jpeg_from_image(file_path, page_temp_paths['background_jpeg'], jpeg_quality)

            # Create empty/transparent foreground and mask (all black, meaning transparent)
            empty_1bit_img = Image.new('1', (page_width, page_height), 0) # 0 for black (transparent text)
            empty_1bit_img.save(page_temp_paths['foreground_tiff_1bit'])
            empty_1bit_img.save(page_temp_paths['mask_tiff_1bit'])
        else:
            raise ValueError(f"Unsupported image mode for {file_path}: {img.mode}")

    if page_width is None or page_height is None:
        raise ValueError(f"Could not determine dimensions for {page_identifier}")

    return page_identifier, page_temp_paths, (page_width, page_height)


# Create a Click group for subcommands
@click.group()
@click.argument("inputdir", type=click.Path(exists=True, file_okay=False))
@click.option("--temp-dir", default=None, type=click.Path(file_okay=False, dir_okay=True, writable=True), envvar='TEMPDIR', help="Temporary directory to store intermediate files.")
@click.option("--keep-temp", default=False, is_flag=True, help="Keep temporary files.")
@click.option("--threads", "-t", default=os.cpu_count(), type=click.INT, help="Number of threads to use for image processing.")
@click_logging.simple_verbosity_option(logger)
@click.pass_context
def cli(ctx, inputdir, temp_dir, keep_temp, threads):
    """
    Assembles a multi-page document (DjVu or PDF) from input images.
    Each page is comprised either of separated input images stored in INPUTDIR/foreground and INPUTDIR/background,
    or of a single input image stored in INPUTDIR.
    """
    ctx.ensure_object(dict)
    ctx.obj['INPUTDIR'] = inputdir
    ctx.obj['TEMP_DIR_OBJ'] = None
    if temp_dir is None:
        ctx.obj['TEMP_DIR_OBJ'] = tempfile.TemporaryDirectory(prefix="doc_assembler_temp_")
        ctx.obj['TEMP_DIR'] = ctx.obj['TEMP_DIR_OBJ'].name
        logger.info(f"Using temporary folder: {ctx.obj['TEMP_DIR']}")
    else:
        ctx.obj['TEMP_DIR'] = temp_dir
        os.makedirs(temp_dir, exist_ok=True) # Ensure temp_dir exists if user specified

    ctx.obj['KEEP_TEMP'] = keep_temp
    ctx.obj['THREADS'] = threads
    ctx.obj['BACKGROUND_DIR'] = os.path.join(inputdir, "background")
    ctx.obj['FOREGROUND_DIR'] = os.path.join(inputdir, "foreground")

    # Collect all image filenames to process. Assume .tif for now.
    image_filenames = sorted([f for f in os.listdir(inputdir) if f.endswith(".tif")])
    if not image_filenames:
        logger.error(f"No .tif image files found in {inputdir}.")
        ctx.abort()
    ctx.obj['IMAGE_FILENAMES'] = image_filenames


@cli.command()
@click.option("--outputfile", "-o", default=None, type=click.Path(file_okay=True, writable=True), help="Output PDF file.")
@click.option("--resolution", "-r", default=300, type=click.INT, help="Resolution of the input images (dpi). Used for PDF page size calculations.")
@click.option("--jpeg-quality", "-q", default=85, type=click.IntRange(0, 100), help="JPEG compression quality for background images (0-100).")
@click.option("--jbig2-losslevel", "-b", default=0, type=click.IntRange(0, 10), help="JBIG2 loss level for bitonal foreground pages (0 for lossless). Higher value means more loss, smaller size.")
@click.pass_context
def pdf(ctx, outputfile, resolution, jpeg_quality, jbig2_losslevel):
    """
    Assembles a multi-page PDF document.
    """
    inputdir = ctx.obj['INPUTDIR']
    temp_dir = ctx.obj['TEMP_DIR']
    keep_temp = ctx.obj['KEEP_TEMP']
    threads = ctx.obj['THREADS']
    background_dir = ctx.obj['BACKGROUND_DIR']
    foreground_dir = ctx.obj['FOREGROUND_DIR']
    image_filenames = ctx.obj['IMAGE_FILENAMES']
    temp_dir_obj = ctx.obj['TEMP_DIR_OBJ']

    if outputfile is None:
        outputfile = os.path.join(inputdir, "output.pdf")
        logger.info(f"Using output file: {outputfile}")

    if os.path.exists(outputfile):
        click.confirm(f"Output file {outputfile} already exists. Do you want to continue?", abort=True)

    processed_pages_data = [] # To store (filename, layer_paths, dimensions)
    all_fg_tiff_paths_for_global_jbig2 = [] # Collect all 1-bit foregrounds for global JBIG2

    # --- Phase 0: Prepare all page layers (parallelized) ---
    logger.info("Phase 0: Extracting and preparing page layers for PDF...")
    
    process_page_partial = partial(
        process_page_for_pdf,
        inputdir=inputdir,
        background_dir=background_dir,
        foreground_dir=foreground_dir,
        temp_dir=temp_dir,
        jpeg_quality=jpeg_quality
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
        futures = [executor.submit(process_page_partial, filename) for filename in image_filenames]
        futures_iterator = tqdm(concurrent.futures.as_completed(futures), total=len(futures), desc="Preparing PDF page layers")

        for future in futures_iterator:
            try:
                original_filename, page_temp_paths, page_dimensions = future.result()
                processed_pages_data.append((original_filename, page_temp_paths, page_dimensions))
                all_fg_tiff_paths_for_global_jbig2.append(page_temp_paths['foreground_tiff_1bit'])
            except Exception as e:
                logger.error(f"Error preparing page {original_filename}: {e}", exc_info=True)
                # If a page fails, we will attempt to continue, but the output PDF might be missing this page.
                # Consider adding a list of failed pages if you want to report them at the end.

    processed_pages_data.sort(key=lambda x: image_filenames.index(x[0])) # Re-sort to original order
    
    if not processed_pages_data:
        logger.error("No pages were successfully processed for PDF assembly.")
        ctx.abort()

    # --- Phase 1: Create global JBIG2 dictionary and then per-page JBIG2 files ---
    jbig2_output_basename = "jbig2_output" 
    jbig2_page_paths_map, global_jbig2_file_path = create_jbig2_global_dict_and_pages(
        temp_dir, all_fg_tiff_paths_for_global_jbig2, jbig2_output_basename, jbig2_losslevel
    )
    
    # Read the global JBIG2 dictionary data if it exists
    global_jbig2_data = None
    if global_jbig2_file_path and os.path.exists(global_jbig2_file_path):
        with open(global_jbig2_file_path, 'rb') as f:
            global_jbig2_data = f.read()
        logger.debug(f"Read global JBIG2 dictionary data from: {global_jbig2_file_path}")
    elif all_fg_tiff_paths_for_global_jbig2: # Only warn if there were foregrounds to begin with
        logger.warning("Global JBIG2 dictionary file was not created, or path is invalid. JBIG2 compression may not be optimal.")


    logger.info("Phase 2: Assembling PDF document...")
    pdf_writer = PdfWriter()

    # Define the global JBIG2 dictionary *once* as a PdfDict.
    # This object will automatically become an indirect object when referenced.
    global_jbig2_dict_obj = None
    if global_jbig2_data:
        global_jbig2_dict_obj = PdfDict(
            Type=PdfName('/JBIG2Globals'),
            stream=global_jbig2_data
        )
        logger.debug("Created global JBIG2 dictionary object.")


    for page_idx, (original_filename, page_temp_paths, page_dimensions) in enumerate(tqdm(processed_pages_data, desc="Assembling PDF pages")):
        page_width_px, page_height_px = page_dimensions
        
        # Convert pixels to PDF points (1 inch = 72 points, assuming DPI)
        page_width_pt = (page_width_px / resolution) * 72
        page_height_pt = (page_height_px / resolution) * 72

        page = PdfDict(
            Type=PdfName('Page'),
            # Parent will be set by PdfWriter later when adding to pages tree
            MediaBox=PdfArray([0, 0, page_width_pt, page_height_pt]),
        )
        # Resources dictionary for images and procset
        resources = PdfDict(
            XObject=PdfDict(),
            ProcSet=PdfArray([PdfName('/PDF'), PdfName('/Text'), PdfName('/ImageB'), PdfName('/ImageC'), PdfName('/ImageI')])
        )
        page.Resources = resources

        # --- Create Background Image Object (JPEG) ---
        bg_jpeg_path = page_temp_paths['background_jpeg']
        bg_xobject_name = None
        if os.path.exists(bg_jpeg_path):
            with open(bg_jpeg_path, 'rb') as f:
                bg_data = f.read()

            bg_xobject = PdfDict(
                Type=PdfName('/XObject'),
                Subtype=PdfName('/Image'),
                Width=page_width_px,
                Height=page_height_px,
                ColorSpace=PdfName('/DeviceRGB'),
                BitsPerComponent=8,
                Filter=PdfName('/DCTDecode'),
                stream=bg_data
            )
            bg_xobject_name = PdfName(f"/ImBG_{page_idx:04d}") # Unique name per page
            resources.XObject[bg_xobject_name] = bg_xobject # Directly assign, pdfrw makes it indirect on write
            logger.debug(f"Added background JPEG XObject {bg_xobject_name}.")
        else:
            logger.warning(f"Background JPEG not found for page {original_filename}. Page may appear incomplete.")

        # --- Create Mask Image Object (for SMask) ---
        mask_tiff_path = page_temp_paths['mask_tiff_1bit']
        mask_xobject_name = None
        if os.path.exists(mask_tiff_path):
            with Image.open(mask_tiff_path) as mask_img_pil:
                mask_data = mask_img_pil.tobytes()
            
            mask_xobject = PdfDict(
                Type=PdfName('/XObject'),
                Subtype=PdfName('/Image'),
                Width=mask_img_pil.width,
                Height=mask_img_pil.height,
                ColorSpace=PdfName('/DeviceGray'),
                BitsPerComponent=8,
                Filter=PdfName('/FlateDecode'), # Common for masks, lossless.
                stream=mask_data
            )
            mask_xobject_name = PdfName(f"/ImMask_{page_idx:04d}") # Unique name per page
            resources.XObject[mask_xobject_name] = mask_xobject # Directly assign
            logger.debug(f"Added mask XObject {mask_xobject_name}.")
        else:
            logger.warning(f"Mask TIFF not found for page {original_filename}. Foreground transparency may be incorrect.")

        # --- Create Foreground Image (JBIG2) Object ---
        fg_tiff_original_path = page_temp_paths['foreground_tiff_1bit']
        fg_jbig2_path = jbig2_page_paths_map.get(fg_tiff_original_path) # Get path from map
        fg_xobject_name = None

        if fg_jbig2_path and os.path.exists(fg_jbig2_path):
            with open(fg_jbig2_path, 'rb') as f:
                fg_data = f.read()

            # The DecodeParms dictionary is where JBIG2Globals is specified.
            # Create it only if we have global data to link.
            decode_parms = None
            if global_jbig2_dict_obj:
                decode_parms = PdfDict(JBIG2Globals=global_jbig2_dict_obj)
                logger.debug("JBIG2 DecodeParms including JBIG2Globals.")
            else:
                logger.debug("No global JBIG2 dictionary, DecodeParms will be omitted.")


            fg_xobject = PdfDict(
                Type=PdfName('/XObject'),
                Subtype=PdfName('/Image'),
                Width=page_width_px,
                Height=page_height_px,
                ColorSpace=PdfName('/DeviceGray'), # JBIG2 is for 1-bit or grayscale
                BitsPerComponent=1,      # For 1-bit JBIG2
                Filter=PdfName('/JBIG2Decode'),
                # Add DecodeParms only if it was created
                **(dict(DecodeParms=decode_parms) if decode_parms else {}),
                stream=fg_data
            )
            fg_xobject_name = PdfName(f"/ImFG_{page_idx:04d}") # Unique name per page
            resources.XObject[fg_xobject_name] = fg_xobject # Directly assign
            logger.debug(f"Added foreground JBIG2 XObject {fg_xobject_name}.")
            
            # Link the foreground image to its soft mask (if available)
            if mask_xobject_name:
                fg_xobject.SMask = resources.XObject[mask_xobject_name] 
                logger.debug(f"Linked foreground {fg_xobject_name} to mask {mask_xobject_name}.")

        else:
            logger.warning(f"Foreground JBIG2 not found for page {original_filename}. Text layer may be missing.")

        # --- Page Content Stream ---
        # Content stream defines how XObjects are placed on the page.
        # Images are placed using the `Do` operator within a graphics state.
        # `q` saves current graphics state, `Q` restores.
        # `width 0 0 height 0 0 cm` sets the transformation matrix to scale
        # an image's natural coordinates (0-1, 0-1) to the desired pixel dimensions
        # on the PDF page (0-width_pt, 0-height_pt).
        
        content_stream_commands = []
        # Background goes first (bottom layer)
        if bg_xobject_name:
            # Assuming bg_xobject is an image, its natural coordinates are 0-1, 0-1.
            # We want to scale it to fill the page (width_pt x height_pt).
            content_stream_commands.append(f"""
            q
            {page_width_pt:.2f} 0 0 {page_height_pt:.2f} 0 0 cm
            {bg_xobject_name} Do
            Q
            """)
            logger.debug(f"Added background draw command for {bg_xobject_name}.")
        
        # Foreground goes on top
        if fg_xobject_name:
            # Similar scaling for foreground
            content_stream_commands.append(f"""
            q
            {page_width_pt:.2f} 0 0 {page_height_pt:.2f} 0 0 cm
            {fg_xobject_name} Do
            Q
            """)
            logger.debug(f"Added foreground draw command for {fg_xobject_name}.")
        
        # Combine commands into a single stream
        content_stream_data = "".join(content_stream_commands).strip() # Remove leading/trailing whitespace
        
        if content_stream_data:
            page.Contents = PdfDict(stream=PdfString.encode(content_stream_data))
            logger.debug(f"Page {page_idx} content stream created with length {len(content_stream_data)}.")
        else:
            logger.warning(f"Page {original_filename} has no content stream commands. Page will be blank.")
            # Optionally, you might want to create a minimal, empty content stream
            # or skip adding this page if it truly has no content.

        pdf_writer.addpage(page)

    pdf_writer.write(outputfile)
    logger.info(f"PDF file '{outputfile}' created successfully!")

    # --- Cleanup ---
    # In pdfrw, objects added to resources.XObject, page.Contents, or other dictionaries
    # that are part of the page/trailer structure are automatically made indirect
    # and written. So explicit deletion of the Python objects is not needed for pdfrw itself,
    # but cleaning up the temporary files created by jbig2enc and PIL is crucial.
    
    # Remove all .sym and .XXXX files created by jbig2enc
    if not keep_temp:
        logger.debug("Cleaning up temporary JBIG2 files.")
        for f in os.listdir(temp_dir):
            if f.startswith(jbig2_output_basename) and (f.endswith(".sym") or f.split('.')[-1].isdigit()): # .0000, .0001 files have no extension like .jb2
                file_to_remove = os.path.join(temp_dir, f)
                try:
                    os.remove(file_to_remove)
                    logger.debug(f"Removed temporary JBIG2 file: {file_to_remove}")
                except OSError as e:
                    logger.warning(f"Could not remove temporary JBIG2 file {file_to_remove}: {e}")
        
        # Remove temporary page image files (JPEG, TIFFs)
        for page_data in processed_pages_data:
            _, page_temp_paths, _ = page_data
            for path_key in ['background_jpeg', 'foreground_tiff_1bit', 'mask_tiff_1bit']:
                file_to_remove = page_temp_paths[path_key]
                if os.path.exists(file_to_remove):
                    try:
                        os.remove(file_to_remove)
                        logger.debug(f"Removed temporary page image file: {file_to_remove}")
                    except OSError as e:
                        logger.warning(f"Could not remove temporary page image file {file_to_remove}: {e}")

        if temp_dir_obj is not None:
            logger.debug("Cleaning up temporary folder.")
            temp_dir_obj.cleanup()
    elif keep_temp:
        logger.info(f"Temporary files kept in: {temp_dir}")


@cli.command()
@click.option("--outputfile", "-o", default=None, type=click.Path(file_okay=True, writable=True), help="Output DjVu file.")
@click.option("--resolution", "-r", default=300, type=click.INT, help="Resolution of the input images (dpi).")
@click.option("--cr-cjb2", "-b", default=1, type=click.INT, help="Compression ratio for bitonal pages (DjVu cjb2 losslevel).")
@click.option("--cr-c44", "-q", default="74,89,99", type=click.STRING, help="Compression quality for DjVu C44 and IW44 layer (e.g., '74,89,99').")
@click.pass_context
def djvu(ctx, outputfile, resolution, cr_cjb2, cr_c44):
    """
    Assembles a multi-page DjVu document.
    """
    inputdir = ctx.obj['INPUTDIR']
    temp_dir = ctx.obj['TEMP_DIR']
    keep_temp = ctx.obj['KEEP_TEMP']
    threads = ctx.obj['THREADS']
    background_dir = ctx.obj['BACKGROUND_DIR']
    foreground_dir = ctx.obj['FOREGROUND_DIR']
    image_filenames = ctx.obj['IMAGE_FILENAMES']
    temp_dir_obj = ctx.obj['TEMP_DIR_OBJ']

    if outputfile is None:
        outputfile = os.path.join(inputdir, "output.djvu")
        logger.info(f"Using output file: {outputfile}")

    if os.path.exists(outputfile):
        click.confirm(f"Output file {outputfile} already exists. Do you want to continue?", abort=True)
    
    pages_to_assemble_djvu = [] # Stores paths to individual DjVu page files

    def process_image_for_djvu(filename):
        """
        Process an image file / a background/foreground image pair (if found) and generates a DjVu file for the page.

        Args:
            filename (str): The name of the image file to process.

        Returns:
            tuple: A tuple containing the original filename and the path of the generated DjVu file.
        """
        file_path = os.path.join(inputdir, filename)
        page_name = os.path.splitext(filename)[0]
        output_path = os.path.join(temp_dir, f"{page_name}.djvu")

        background_path = os.path.join(background_dir, f"{page_name}.tif")
        foreground_path = os.path.join(foreground_dir, f"{page_name}.tif")

        if os.path.exists(background_path) and os.path.exists(foreground_path):
            # Process foreground and background files
            logger.debug(f"DjVu: Processing page {page_name} with background and foreground files.")

            # Convert background to PAM format
            background_pam_data, _, _ = run_command(["tifftopnm", background_path])

            # Convert foreground to RLE format
            foreground_pbm_data, _, _ = run_command(["tifftopnm", foreground_path])
            foreground_rle_data, _, _ = run_command(["pbmtodjvurle"], input_data=foreground_pbm_data)

            # Run csepdjvu to combine background and foreground
            combined_data = foreground_rle_data + background_pam_data
            run_command(["csepdjvu", "-d", str(resolution), "-q", cr_c44, "-", output_path], input_data=combined_data)

            return filename, output_path
        else:
            # Process single file only
            logger.debug(f"DjVu: Processing page {page_name} with single file.")
            img = Image.open(file_path)

            if img.mode == "1":
                # Process bitonal page
                logger.debug("DjVu: Bitonal page")
                run_command(["cjb2", "-dpi", str(resolution), "-losslevel", str(cr_cjb2), file_path, output_path])

                return filename, output_path
            elif (img.mode == 'RGB' or img.mode == 'L' or img.mode == 'P'):
                # Process photo page
                logger.debug(f"DjVu: {img.mode} photo page")
                with tempfile.NamedTemporaryFile(prefix="djvu_temp_", dir=temp_dir, delete=False) as temp_file:
                    temp_tiff_path = temp_file.name
                img.save(temp_tiff_path, "TIFF") # Ensure TIFF for tifftopnm

                tiff_to_pbm_data, _, _ = run_command(["tifftopnm", temp_tiff_path])
                os.remove(temp_tiff_path) # Clean up temp TIFF

                run_command(["c44", "-dpi", str(resolution), "-slice", cr_c44, "-", output_path], input_data=tiff_to_pbm_data)
                return filename, output_path
            else:
                logger.error(f"DjVu: Unsupported image mode for {file_path}: {img.mode}")
                return filename, None # Indicate no output for this page

    logger.info("Assembling DjVu document...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
        futures = [executor.submit(process_image_for_djvu, filename) for filename in image_filenames]
        futures_iterator = tqdm(concurrent.futures.as_completed(futures), total=len(futures), desc="Processing DjVu pages")

        for future in futures_iterator:
            filename = None
            try:
                filename, page_path = future.result()
                if page_path:
                    pages_to_assemble_djvu.append((filename, page_path))
            except Exception as e:
                logger.error(f"Error processing DjVu page {filename}: {e}", exc_info=True)
    
    # Sort pages to assemble DjVu in correct order
    pages_to_assemble_djvu.sort(key=lambda x: image_filenames.index(x[0]))
    pages_to_assemble_djvu = [page_path for _, page_path in pages_to_assemble_djvu]

    # Assemble all pages into a single DjVu file
    if pages_to_assemble_djvu:
        if os.path.exists(outputfile):
            logger.debug("Removing old output file.")
            os.remove(outputfile)
        run_command(["djvm", "-c", outputfile] + pages_to_assemble_djvu)
        logger.info(f"DjVu file '{outputfile}' created successfully!")
    else:
        logger.error("No pages found to assemble DjVu file.")
    
    # --- Cleanup ---
    if not keep_temp and temp_dir_obj is not None:
        logger.debug("Cleaning up temporary folder.")
        temp_dir_obj.cleanup()
    elif keep_temp:
        logger.info(f"Temporary files kept in: {temp_dir}")


if __name__ == "__main__":
    cli()