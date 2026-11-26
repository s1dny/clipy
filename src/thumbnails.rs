//! Thumbnails: a picture on the left, and on the white right side an
//! illustration of the clip's idea drawn by the image model.
//!
//! The left is the channel's fixed template (thumbnails/<file>) when it has
//! one, otherwise the clip speaker's headshot with a soft shadow. Headshots
//! are made by the agent itself and shared by every channel.

use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use anyhow::{Context, Result, bail};
use base64::Engine;
use image::buffer::ConvertBuffer;
use image::{
    DynamicImage, GrayImage, ImageReader, Luma, Rgb, RgbImage, RgbaImage, imageops::FilterType,
};
use serde_json::{Value, json};

use crate::app::App;
use crate::config::ChannelConfig;

const SIZE: (u32, u32) = (1920, 1080);
const PANEL: (u32, u32) = (720, 1080);
const ILLUSTRATION: u32 = 700;
const SHADOW: u32 = 80;
/// Every channel at least this bright counts as white.
const WHITE: u8 = 240;

/// The headshot file for a speaker: <data>/headshots/<slug>.jpg.
pub fn headshot_path(app: &App, speaker: &str) -> Result<PathBuf> {
    let mut slug = String::new();
    for c in speaker.to_lowercase().chars() {
        if c.is_ascii_lowercase() || c.is_ascii_digit() {
            slug.push(c);
        } else if !slug.ends_with('-') {
            slug.push('-');
        }
    }
    let slug = slug.trim_matches('-');
    if slug.is_empty() {
        bail!("A speaker needs a name.");
    }
    Ok(app.headshots_dir().join(format!("{slug}.jpg")))
}

/// The speaker's headshot filling the panel: the widest 2:3 portrait of it,
/// centred across and kept near the top so the head stays in. Decoding and
/// resizing run off the async runtime.
pub async fn headshot_panel(app: &App, speaker: &str) -> Result<RgbImage> {
    let path = headshot_path(app, speaker)?;
    if !path.exists() {
        bail!("No headshot of {speaker}: find a photo and save it with the headshot tool.");
    }
    tokio::task::spawn_blocking(move || panel(&path)).await?
}

fn panel(path: &Path) -> Result<RgbImage> {
    let image = open_oriented(path)?.to_rgb8();
    let (w, h) = image.dimensions();
    let (x, y, cw, ch) = if w as u64 * PANEL.1 as u64 > h as u64 * PANEL.0 as u64 {
        // Too wide: trim the sides.
        let width = h * PANEL.0 / PANEL.1;
        ((w - width) / 2, 0, width, h)
    } else {
        // Too tall: trim mostly from the bottom.
        let height = w * PANEL.1 / PANEL.0;
        (0, (h - height) / 10, w, height)
    };
    let cropped = image::imageops::crop_imm(&image, x, y, cw, ch).to_image();
    Ok(image::imageops::resize(
        &cropped,
        PANEL.0,
        PANEL.1,
        FilterType::Lanczos3,
    ))
}

/// A crop box as fractions of the image's width and height.
#[derive(Debug, Clone, Copy, serde::Deserialize, schemars::JsonSchema)]
pub struct Crop {
    pub left: f64,
    pub top: f64,
    pub width: f64,
    pub height: f64,
}

/// Save a speaker's headshot from a photo, optionally cropped first. The
/// thumbnail panel takes the widest 2:3 portrait of it, head near the top.
pub async fn save_headshot(
    app: &App,
    speaker: &str,
    url: &str,
    crop: Option<Crop>,
) -> Result<serde_json::Value> {
    let path = headshot_path(app, speaker)?;
    let image = crate::cards::fetch_image(&app.http, url).await?.to_rgb8();
    let (w, h) = image.dimensions();
    let image = match crop {
        None => image,
        Some(c) => {
            let inside = |start: f64, size: f64| {
                (0.0..1.0).contains(&start) && size > 0.0 && start + size <= 1.0 + 1e-9
            };
            if !inside(c.left, c.width) || !inside(c.top, c.height) {
                bail!("The crop box must lie inside the image: fractions from 0 to 1.");
            }
            let x = (c.left * w as f64).round() as u32;
            let y = (c.top * h as f64).round() as u32;
            let cw = ((c.width * w as f64).round() as u32).min(w - x);
            let ch = ((c.height * h as f64).round() as u32).min(h - y);
            image::imageops::crop_imm(&image, x, y, cw, ch).to_image()
        }
    };
    let (cw, ch) = image.dimensions();
    let portrait_width = cw.min(ch * PANEL.0 / PANEL.1);
    if portrait_width < 480 {
        bail!(
            "The headshot would be {cw}x{ch}; its 2:3 portrait must be at least 480x720. Use a larger photo or a larger crop."
        );
    }
    let target = path.clone();
    tokio::task::spawn_blocking(move || -> Result<()> {
        let mut bytes = Vec::new();
        image::codecs::jpeg::JpegEncoder::new_with_quality(&mut bytes, 95).encode_image(&image)?;
        crate::app::write_atomic(&target, &bytes, None)
    })
    .await??;
    Ok(serde_json::json!({
        "speaker": speaker,
        "photo": url,
        "photo_size": [w, h],
        "saved_size": [cw, ch],
    }))
}

/// The thumbnail panel for a speaker, as a JPEG.
pub async fn panel_jpeg(app: &App, speaker: &str) -> Result<Vec<u8>> {
    let panel = headshot_panel(app, speaker).await?;
    let mut bytes = Vec::new();
    image::codecs::jpeg::JpegEncoder::new_with_quality(&mut bytes, 90).encode_image(&panel)?;
    Ok(bytes)
}

fn open_oriented(path: &Path) -> Result<DynamicImage> {
    use image::ImageDecoder;
    let mut decoder = ImageReader::open(path)?
        .with_guessed_format()?
        .into_decoder()?;
    let orientation = decoder.orientation()?;
    let mut image = DynamicImage::from_decoder(decoder)?;
    image.apply_orientation(orientation);
    Ok(image)
}

/// The white canvas with the headshot panel's soft shadow. It is the same for
/// every headshot thumbnail, so it is blurred once.
static SHADOWED: LazyLock<RgbImage> = LazyLock::new(|| {
    let mut canvas = RgbImage::from_pixel(SIZE.0, SIZE.1, Rgb([255, 255, 255]));
    let mut shade = GrayImage::new(SIZE.0, SIZE.1);
    for y in 0..SIZE.1 {
        for x in 0..(PANEL.0 + SHADOW / 3).min(SIZE.0) {
            shade.put_pixel(x, y, Luma([255]));
        }
    }
    let shade = image::imageops::blur(&shade, SHADOW as f32 / 3.0);
    for (x, y, pixel) in canvas.enumerate_pixels_mut() {
        let a = shade.get_pixel(x, y)[0] as u32;
        for c in 0..3 {
            pixel[c] = ((pixel[c] as u32 * (255 - a) + 150 * a) / 255) as u8;
        }
    }
    canvas
});

/// The thumbnail before its illustration.
async fn left(app: &App, config: &ChannelConfig, speaker: Option<&str>) -> Result<RgbImage> {
    let Some(template) = &config.thumbnail_template else {
        let speaker = speaker.context("This channel's thumbnails need the clip's speaker.")?;
        let panel = headshot_panel(app, speaker).await?;
        return tokio::task::spawn_blocking(move || {
            let mut canvas = SHADOWED.clone();
            image::imageops::replace(&mut canvas, &panel, 0, 0);
            canvas
        })
        .await
        .map_err(Into::into);
    };
    let path = app.data.join("thumbnails").join(template);
    tokio::task::spawn_blocking(move || {
        let image = image::open(&path).with_context(|| format!("open {}", path.display()))?;
        Ok(image
            .resize_exact(SIZE.0, SIZE.1, FilterType::Lanczos3)
            .to_rgb8())
    })
    .await?
}

async fn illustration(
    app: &App,
    prompt: &str,
    config: &ChannelConfig,
    model: &str,
) -> Result<RgbaImage> {
    let key = std::env::var("OPENROUTER_API_KEY").context("OPENROUTER_API_KEY is not set")?;
    let content = config
        .illustration_prompt_template
        .replace("{prompt}", prompt);
    let response = app
        .http
        .post("https://openrouter.ai/api/v1/chat/completions")
        .bearer_auth(key)
        .timeout(std::time::Duration::from_secs(120))
        .json(&json!({
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "modalities": ["image"],
            "provider": {"sort": "price"},
        }))
        .send()
        .await?;
    let status = response.status();
    let body: Value = response.json().await.unwrap_or_default();
    if !status.is_success() {
        bail!(
            "OpenRouter error {status}: {}",
            crate::app::tail(&body.to_string(), 500)
        );
    }
    if let Some(error) = body.get("error") {
        bail!(
            "OpenRouter error: {}",
            error.get("message").unwrap_or(error)
        );
    }
    for image in body["choices"][0]["message"]["images"]
        .as_array()
        .into_iter()
        .flatten()
    {
        let url = image["image_url"]["url"].as_str().unwrap_or_default();
        if let Some((_, data)) = url
            .strip_prefix("data:image")
            .and_then(|rest| rest.split_once(','))
        {
            let bytes = base64::engine::general_purpose::STANDARD.decode(data)?;
            return Ok(image::load_from_memory(&bytes)?.to_rgba8());
        }
    }
    bail!("The image model returned no image.")
}

/// Crop the illustration to its non-white content, scale it, and centre it
/// in the white area right of the picture.
pub fn compose(left: &RgbImage, illustration: &RgbaImage) -> Result<RgbImage> {
    let (mut x0, mut y0, mut x1, mut y1) = (u32::MAX, u32::MAX, 0, 0);
    for (x, y, p) in illustration.enumerate_pixels() {
        if p[3] > 10 && (p[0] < 220 || p[1] < 220 || p[2] < 220) {
            (x0, y0, x1, y1) = (x0.min(x), y0.min(y), x1.max(x), y1.max(y));
        }
    }
    if x0 == u32::MAX {
        bail!("The illustration is blank.");
    }
    let mut drawn =
        image::imageops::crop_imm(illustration, x0, y0, x1 - x0 + 1, y1 - y0 + 1).to_image();
    if drawn.width() > ILLUSTRATION || drawn.height() > ILLUSTRATION {
        drawn = DynamicImage::ImageRgba8(drawn)
            .resize(ILLUSTRATION, ILLUSTRATION, FilterType::Lanczos3)
            .to_rgba8();
    }
    // The picture ends at the last column that is not entirely white.
    let start = (0..left.width())
        .rev()
        .find(|&x| (0..left.height()).any(|y| left.get_pixel(x, y).0.iter().any(|&c| c < WHITE)))
        .map_or(0, |x| x + 1);
    let x = (start + SIZE.0) as i64 / 2 - drawn.width() as i64 / 2;
    let y = SIZE.1 as i64 / 2 - drawn.height() as i64 / 2;
    let mut thumbnail: RgbaImage = left.convert();
    image::imageops::overlay(&mut thumbnail, &drawn, x, y);
    Ok(thumbnail.convert())
}

/// Draw a thumbnail for `prompt` into `path` (a JPEG).
pub async fn draw(
    app: &App,
    channel: &str,
    prompt: &str,
    speaker: Option<&str>,
    path: &Path,
) -> Result<()> {
    let config = app.channel_config(channel)?;
    // Fails fast, before paying for an image.
    let left = left(app, &config, speaker).await?;
    let illustration = illustration(app, prompt, &config, &app.config.image_model).await?;
    let path = path.to_owned();
    tokio::task::spawn_blocking(move || -> Result<()> {
        let thumbnail = compose(&left, &illustration)?;
        std::fs::create_dir_all(path.parent().context("thumbnail folder")?)?;
        let mut bytes = Vec::new();
        image::codecs::jpeg::JpegEncoder::new_with_quality(&mut bytes, 95)
            .encode_image(&thumbnail)?;
        crate::app::write_atomic(&path, &bytes, None)
    })
    .await?
}

/// Whether a thumbnail could be drawn for this channel and speaker without
/// the agent first making a headshot.
pub async fn check_artwork(app: &App, config: &ChannelConfig, speaker: &str) -> Result<()> {
    if config.thumbnail_template.is_none() {
        headshot_panel(app, speaker).await?;
    }
    Ok(())
}
