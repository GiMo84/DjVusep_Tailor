# DjVusep Tailor

DjVusep Tailor is a Python script that assembles a DjVu file from multiple TIFF images. Each page can either be comprised of a single image or a pair of background and foreground images (stored respectively in `/background` and `/foreground`), making it ideal for assembling output files created by [ScanTailor Advanced](https://github.com/4lex4/scantailor-advanced) in “Mixed mode” (e.g. containing background RGB/grayscale graphic and foreground B/W text).

## Requirements

- Python 3
  - `tqdm`
  - `Pillow`
  - `click`
  - `click_logging`
- DjVuLibre
  - `cjb2`
  - `pngtopam`
  - `tifftopnm`
  - `pbmtodjvurle`
  - `csepdjvu`
  - `djvm`

## Installation

You can install the required Python packages using the following command:

```shell
pip install -r requirements.txt
```

The DjVuLibre tools can be obtained from [DjVuLibre's website](https://djvu.sourceforge.net/) or from packages in your Linux's distribution (e.g., in Ubuntu, `djvulibre-bin`).

DjVusep Tailor expects the DjVuLibre files to be in the PATH.

## Usage

```shell
python djvusep_taylor.py INPUTDIR [options]
```

### Options

- `--outputfile, -o`: Output file.
- `--resolution, -r`: Resolution of the input images (dpi).
- `--cr-cjb2, -b`: Compression ratio for bitonal pages (see [cjb2](https://linux.die.net/man/1/cjb2)’s man page).
- `--cr-c44, -q`: Compression quality for the C44 and IW44 layer (see [c44](https://linux.die.net/man/1/c44)’s man page).
- `--temp-dir`: Temporary directory to store intermediate files.
- `--keep-temp`: Keep temporary files.
- `--threads, -t`: Number of threads to use for image processing.
- `--verbosity, -v`: Either CRITICAL, ERROR, WARNING, INFO or DEBUG.

## Example

```shell
python djvusep_taylor.py --resolution 600 --cr-cjb2 100 --cr-c44 60+12+10 --threads 4 --outfile out.djvu my_images
```

This will process images from the `my_images` folder, using a resolution of 600x600 dpi, cjb2 compression ratio of 100, C44/IW44 compression quality of 60+12+10, and 4 image compression threads to create `out.djvu`.

For more information on the available options, you can use `python djvusep_taylor.py --help`.

## License

This project is licensed under the GPL-3.0 license - see the [LICENSE](LICENSE) file for details.