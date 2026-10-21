# Video Downloader (Windows 10/11 x64)

Paste a video link, see the thumbnail, title and length, pick a quality, choose a folder, download.
One fixed screen with large touch-friendly buttons and no scroll bars, made for a ROG Ally X at 1920x1080 / 150 %.

## Run
1. Download `VideoDownloader-<version>-win64.zip` from Releases and unzip it.
2. Open the `VideoDownloader` folder and run **VideoDownloader.exe**.
3. The build is unsigned. On the SmartScreen warning, choose **More info**, then **Run anyway**.

## Use
- **Links box:** paste one link or several, one per line. **Paste** pastes from the clipboard. Then press **Fetch**.
- **Quality:** Best, 1080p, 720p, 480p, MP3 (audio) or M4A (audio). You can also change it per video in the queue.
- **Whole playlist:** a playlist or channel link adds every video to the queue. The queue shows 5 at a time; use Prev/Next.
- **Scan page for videos:** for normal web pages, it looks for `<video>` players and `.mp4`/`.webm`/`.m3u8` links in the page.
- **Cookies from browser (Edge/Chrome/Firefox):** only for videos you can already watch while signed in, such as
  private-to-you, age-restricted or members-only videos. Close that browser first. The cookies are only used for that site.
- **Browse** picks the save folder; it is remembered. **Download** saves the queue in order, with progress, speed and
  time left. **Cancel** stops the current video and deletes its partial file. **Open folder** opens the save folder.
- **Update downloader engine:** sites change often. This fetches the newest yt-dlp from PyPI, checks its SHA-256 and
  uses it after a restart. If an update ever fails to load, the built-in engine is used automatically.

Files are saved as MP4 (H.264/AAC when the site offers it). A WebM-only source is stream-copied into MP4 when possible.

## Limits
- DRM-protected videos (Netflix, Prime Video, Disney+, Spotify, most paid TV) can't be downloaded. The app says so and does not bypass DRM.
- Region-locked videos are not unlocked.
- Live streams are not recorded.
- Vimeo currently requires sign-in for most videos. Use Cookies from browser.
- Only download videos you have the right to save (your own, Creative Commons, public domain, or permitted by the site).

## Build / test
CI (`.github/workflows/build.yml`) builds on windows-latest with PyInstaller. It bundles FFmpeg (LGPL) and QuickJS, then runs
the packaged exe's `--selftest`: a local server with a fixture MP4, an HLS stream, a page with `<video>` tags, a slow file
(Cancel), and 403/404/no-video pages. Every result is checked with ffprobe. It also runs the real window through a queue.
