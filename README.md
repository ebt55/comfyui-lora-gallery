# ComfyUI LoRA Gallery

**Version 1.0.0** · A local-first web gallery for browsing images created with ComfyUI and filtering them by the LoRAs recorded in their embedded metadata.

The application runs on your computer, scans one folder recursively, and opens a browser-based gallery. It does not require a running ComfyUI instance, a database, an account, or a frontend build step.

## Features

- Scans PNG, WebP, JPEG, and JPG images recursively.
- Reads standard ComfyUI `prompt` and `workflow` metadata, plus common JPEG/WebP EXIF fields.
- Detects standard LoRA loader nodes, many custom-node layouts, and `<lora:name:weight>` prompt tags.
- Ignores LoRA entries explicitly disabled with `on`, `enabled`, or `active` set to false.
- Shows LoRA names and available model, CLIP, and weight strengths.
- Filters selected LoRAs with **Match any** or **Match all**.
- Searches filenames and LoRA names; sorts by date, filename, or LoRA count.
- Opens an image detail view with its extracted LoRAs and raw embedded metadata.
- Shows live discovery and scan progress; supports rescanning without restarting.
- Generates thumbnails only in the operating system's temporary cache. Source images are not modified.

## Requirements

- Python 3.10 or newer
- `pip`

## Install and run

Create and activate an isolated environment (recommended):

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

On macOS or Linux, activate it with:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Start the gallery, replacing the placeholder with the folder that contains your ComfyUI images:

```text
python app.py "<output-directory>"
```

The browser normally opens automatically. If it does not, visit `http://127.0.0.1:8787/`.

## Command-line options

```text
python app.py [DIRECTORY] [--host 127.0.0.1] [--port 8787] [--no-open]
python app.py --version
```

`DIRECTORY` defaults to the current directory. Keep the default host, `127.0.0.1`, unless everyone on the network is trusted: choosing another host exposes the selected images, filenames, and metadata to devices that can reach the server.

## Filtering behavior

When more than one LoRA is selected:

- **Match any** shows an image if it contains at least one selected LoRA.
- **Match all** shows an image only if it contains every selected LoRA.

For example, selecting `LoRA_A`, `LoRA_B`, and `LoRA_C` with **Match any** returns images containing A, B, or C. **Match all** returns only images that contain A, B, and C together.

## Metadata compatibility and limitations

This project is not specific to Krea2. It works with any LoRA that can be identified from ComfyUI metadata, including standard `LoraLoader`-style nodes and several common custom-node shapes.

- Metadata support is best for images whose ComfyUI workflow/prompt data was preserved. Images exported through services or editors that strip metadata may show no LoRAs.
- Custom nodes can use arbitrary field names and nested structures. Some will not be recognized until a parser rule is added to `extract_loras()` in `app.py`.
- JPEG/WebP metadata conventions vary more than ComfyUI PNG metadata, so detection can be incomplete.
- The application reads and displays the original image, filename, relative folder name, and embedded metadata in the local browser. This can include prompts, model names, workflow details, and other sensitive information contained in your images.
- It is a local browsing tool, not an access-control system. It does not provide authentication, HTTPS, multi-user permissions, cloud sync, or a persistent index.
- Very large folders can take time to discover and scan. The gallery scans again when you choose **Rescan folder**.

## Privacy and responsible use

No image files, generated thumbnails, workflow exports, model files, local data directories, environment files, or common credential files are tracked by default. Review `git status --ignored` before publishing changes, especially if you intentionally force-add files.

This is a general-purpose local metadata viewer. You are responsible for the images, prompts, model files, metadata, sharing choices, and compliance with applicable law and platform rules. The project provides no guarantee about content, metadata detection, privacy, or fitness for a particular purpose. Do not expose the gallery beyond localhost unless you understand the implications.

## License

Licensed under the [Apache License 2.0](LICENSE). The software is provided on an “AS IS” basis, without warranties or conditions of any kind. See the license for the complete terms and limitations of liability.
