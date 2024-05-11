"""
DjVusep Tailor assembles a single DjVu file by converting a series of images into its pages
Copyright (C) 2024 Giulio Molinari

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

import os
import subprocess
import logging
import click
import click_logging
import tempfile
from PIL import Image
import concurrent.futures
from tqdm import tqdm

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
        logger.debug(f"Running command: {command}")
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
        )
        stdout, stderr = process.communicate(input_data)
        returncode = process.returncode
        logger.debug(f"{os.path.basename(command[0])} [{str(returncode)}]: {stderr.decode()}")
        if len(stdout) > 0:
            logger.debug(f"{os.path.basename(command[0])} returned {len(stdout)} bytes.")
        if returncode != 0:
            raise CommandError("Command {} failed with return code {}: {}".format(command, returncode), stderr.decode())
        return stdout, stderr, process
    except Exception as e:
        raise CommandError("Error running command {}: {}".format(command, str(e)), "")

@click.command()
@click.argument("inputdir", type=click.Path(exists=True))
@click.option("--outputfile", "-o", default=None, type=click.Path(file_okay=True, writable=True), help="Output file.")
@click.option("--resolution", "-r", default=300, type=click.INT, help="Resolution of the input images (dpi).")
@click.option("--cr-cjb2", "-b", default=1, type=click.INT,help="Compression ratio for bitonal pages.")
@click.option("--cr-c44", "-q", default="74,89,99", type=click.STRING, help="Compression quality for the C44 and IW44 layer.")
@click.option("--temp-dir", default=None, type=click.Path(file_okay=False, dir_okay=True, writable=True), envvar='TEMPDIR', help="Temporary directory to store intermediate files.")
@click.option("--keep-temp", default=False, is_flag=True, help="Keep temporary files.")
@click.option("--threads", "-t", default=1, type=click.INT, help="Number of threads to use for image processing.")
@click_logging.simple_verbosity_option(logger)
def main(inputdir, outputfile, resolution, cr_cjb2, cr_c44, temp_dir, keep_temp, threads):
    """
    DjVusep Tailor assembles a single DjVu file by converting a series of images into its pages.
    Each page is comprised either of separated input images stored in INPUTDIR/foreground and INPUTDIR/background, or of a single input image stored in INPUTDIR.
    """
    background_dir = os.path.join(inputdir, "background")
    foreground_dir = os.path.join(inputdir, "foreground")
    if outputfile is None:
        outputfile = os.path.join(inputdir, "output.djvu")
        logger.info(f"Using output file: {outputfile}")

    if os.path.exists(outputfile):
        click.confirm(f"Output file {outputfile} already exists. Do you want to continue?", abort=True)

    temp_dir_obj = None
    if temp_dir is None:
        temp_dir_obj = tempfile.TemporaryDirectory(prefix="djvu_temp_")
        temp_dir = temp_dir_obj.name
        logger.info(f"Using temporary folder: {temp_dir}")

    pages = []

    def process_image(filename):
        """
        Process an image file / a background/foreground image pair (if found) and generates a DjVu file for the page.

        Args:
            filename (str): The name of the image file to process.

        Returns:
            tuple: A tuple containing the original filename and the path of the generated DjVu file.
        """
        file_path = os.path.join(inputdir, filename)
        if filename.endswith(".tif"):
            page_name = os.path.splitext(filename)[0]
            background_path = os.path.join(background_dir, f"{page_name}.tif")
            foreground_path = os.path.join(foreground_dir, f"{page_name}.tif")
            output_path = os.path.join(temp_dir, f"{page_name}.djvu")

            if os.path.exists(background_path) and os.path.exists(foreground_path):
                # Process foreground and background files
                logger.debug(f"Processing page {page_name} with background and foreground files.")

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
                logger.debug(f"Processing page {page_name} with single file.")
                img = Image.open(file_path)

                if img.mode == "1":
                    # Process bitonal page
                    logger.debug("Bitonal page")
                    run_command(["cjb2", "-dpi", str(resolution), "-losslevel", str(cr_cjb2), file_path, output_path])

                    return filename, output_path
                elif (img.mode == 'RGB' or img.mode == 'L'):
                    # Process photo page
                    if img.mode == 'RGB':
                        logger.debug("RGB page")
                    else:
                        logger.debug("Grayscale page")
                    with tempfile.NamedTemporaryFile(prefix="djvu_temp_", dir=temp_dir) as temp_file:
                        logger.debug(f"Writing image to temporary file {temp_file.name}")
                        tiff_to_pbm_data, _, _ = run_command(["tifftopnm", file_path])

                        with open(temp_file.name, "wb") as f:
                            f.write(tiff_to_pbm_data)

                        run_command(["c44", "-dpi", str(resolution), "-slice", cr_c44, temp_file.name, output_path])
                    return filename, output_path

    # Process images from input directory
    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
        futures = [executor.submit(process_image, filename) for filename in sorted(os.listdir(inputdir))]
        if logger.level >= logging.INFO:
            # Enable progress bar
            futures_iterator = tqdm(concurrent.futures.as_completed(futures), total=len(futures), desc="Processing pages")
        else:
            # Disable progress bar: debug output would ruin it
            futures_iterator = concurrent.futures.as_completed(futures)

        for future in futures_iterator:
            if future.result() is not None:
                filename, page = future.result()
                if page:
                    pages.append((filename, page))

    # Restore pages order
    pages.sort(key=lambda x: sorted(os.listdir(inputdir)).index(x[0]))
    pages = [page for _, page in pages]

    # Assemble all pages into a single DjVu file
    if pages:
        if os.path.exists(outputfile):
            logger.debug("Removing old output file.")
            os.remove(outputfile)
        run_command(["djvm", "-c", outputfile] + pages)
        logger.info(f"DjVu file '{outputfile}' created successfully!")
        if not keep_temp and temp_dir_obj is not None:
            logger.debug("Cleaning up temporary folder.")
            temp_dir_obj.cleanup()
    else:
        logger.error("No pages found to assemble DjVu file.")


if __name__ == "__main__":
    main()
