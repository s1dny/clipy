//! The approved on-screen treatment: square image-and-caption cards and a
//! persistent source/date marker, in monospace type. Category labels are
//! never drawn.

use std::path::{Path, PathBuf};

use ab_glyph::{Font, FontRef, PxScale, ScaleFont};
use anyhow::{Context, Result, bail};
use image::{DynamicImage, ImageDecoder, ImageReader, Rgba, RgbaImage, imageops::FilterType};
use imageproc::rect::Rect;

use crate::clips::{Annotation, Position, SourceAttribution};

static REGULAR: &[u8] = include_bytes!("../assets/DejaVuSansMono.ttf");
static BOLD: &[u8] = include_bytes!("../assets/DejaVuSansMono-Bold.ttf");
const MAX_IMAGE_BYTES: usize = 10_000_000;

struct Face {
    font: FontRef<'static>,
    scale: PxScale,
}

impl Face {
    /// A font at an em size in pixels.
    fn new(bold: bool, size: u32) -> Self {
        let font =
            FontRef::try_from_slice(if bold { BOLD } else { REGULAR }).expect("bundled font");
        let scale = font.pt_to_px_scale(size as f32).expect("scalable font");
        Self { font, scale }
    }

    fn width(&self, text: &str) -> f32 {
        let scaled = self.font.as_scaled(self.scale);
        let mut width = 0.0;
        let mut previous = None;
        for c in text.chars() {
            let glyph = scaled.glyph_id(c);
            if let Some(previous) = previous {
                width += scaled.kern(previous, glyph);
            }
            width += scaled.h_advance(glyph);
            previous = Some(glyph);
        }
        width
    }

    fn draw(&self, image: &mut RgbaImage, x: i32, y: i32, color: [u8; 3], text: &str) {
        let color = Rgba([color[0], color[1], color[2], 255]);
        imageproc::drawing::draw_text_mut(image, color, x, y, self.scale, &self.font, text);
    }
}

fn round(value: f64) -> i64 {
    value.round() as i64
}

fn fill(image: &mut RgbaImage, left: i64, top: i64, right: i64, bottom: i64, color: [u8; 4]) {
    // Inclusive corners, as the box is specified.
    let rect = Rect::at(left as i32, top as i32).of_size(
        (right - left + 1).max(1) as u32,
        (bottom - top + 1).max(1) as u32,
    );
    imageproc::drawing::draw_filled_rect_mut(image, rect, Rgba(color));
}

/// Draw one card as a full-frame transparent PNG at `path`.
pub fn card(
    card: &Annotation,
    art: Option<DynamicImage>,
    width: u32,
    height: u32,
    path: &Path,
) -> Result<()> {
    let (w, h) = (width as f64, height as f64);
    let size = round(h * 0.025).max(10) as u32;
    let font = Face::new(false, size);
    let heading_font = Face::new(true, round(size as f64 * 1.12) as u32);
    let margin = round(w * 0.025).max(8);
    let pad = round(h * 0.017).max(8);
    let limit = (width as i64 - 2 * margin).min(round(w * 0.80));
    let art_width = if card.image_url.is_some() {
        round(w * 0.17)
    } else {
        0
    };
    let art_height = round(art_width as f64 * 9.0 / 16.0);
    let inset = if art_width > 0 { art_width + pad } else { 0 };
    let text_width = (limit - 2 * pad - inset) as f32;

    let mut lines = Vec::new();
    for paragraph in card.text.lines() {
        let mut line = String::new();
        for word in paragraph.split_whitespace() {
            let trial = if line.is_empty() {
                word.to_owned()
            } else {
                format!("{line} {word}")
            };
            if font.width(&trial) > text_width {
                if line.is_empty() {
                    bail!("Card contains a word too wide to display.");
                }
                lines.push(std::mem::replace(&mut line, word.to_owned()));
            } else {
                line = trial;
            }
        }
        if !line.is_empty() {
            lines.push(line);
        }
    }
    if lines.is_empty() || lines.len() > 9 {
        bail!(
            "Card must fit in nine readable lines; split lengthy explanations into separate cards."
        );
    }
    let heading = card.heading.as_deref().unwrap_or("");
    let heading_height = if heading.is_empty() {
        0
    } else {
        round(size as f64 * 1.5)
    };
    if heading_font.width(heading) > text_width {
        bail!("Card heading is too wide; use a shorter heading.");
    }
    let line_height = round(size as f64 * 1.4);
    let box_height = 2 * pad + art_height.max(heading_height + lines.len() as i64 * line_height);
    if box_height as f64 > h * 0.38 {
        bail!("Card would obscure too much of the original video.");
    }
    let content_width = lines
        .iter()
        .map(|line| font.width(line))
        .chain([heading_font.width(heading)])
        .fold(0.0_f32, f32::max);
    let box_width = limit.min(round(content_width as f64) + 2 * pad + inset);
    let left = match card.position {
        Position::TopLeft | Position::BottomLeft => margin,
        Position::TopRight | Position::BottomRight => width as i64 - margin - box_width,
    };
    let top = match card.position {
        Position::TopLeft | Position::TopRight => round(h * 0.15),
        Position::BottomLeft | Position::BottomRight => height as i64 - margin - box_height,
    };
    let mut image = RgbaImage::new(width, height);
    fill(
        &mut image,
        left,
        top,
        left + box_width,
        top + box_height,
        [15, 18, 22, 238],
    );
    let text_x = (left + pad + inset) as i32;
    if !heading.is_empty() {
        heading_font.draw(
            &mut image,
            text_x,
            (top + pad) as i32,
            [250, 250, 250],
            heading,
        );
    }
    for (i, line) in lines.iter().enumerate() {
        let y = top + pad + heading_height + i as i64 * line_height;
        font.draw(&mut image, text_x, y as i32, [225, 228, 232], line);
    }
    if let Some(art) = art {
        // Keep the full image: fit it inside the panel without cropping.
        let art = art
            .resize(art_width as u32, art_height as u32, FilterType::Lanczos3)
            .to_rgba8();
        let y = top + pad + ((art_height.max(box_height - 2 * pad)) - art.height() as i64) / 2;
        image::imageops::overlay(&mut image, &art, left + pad, y);
    }
    image
        .save(path)
        .with_context(|| format!("save {}", path.display()))?;
    Ok(())
}

/// The persistent "21 July 2024 · Source" marker in the top right.
pub fn source_bug(source: &SourceAttribution, width: u32, height: u32, path: &Path) -> Result<()> {
    let (w, h) = (width as f64, height as f64);
    let font = Face::new(true, round(h * 0.021).max(9) as u32);
    let text = format!(
        "{} · {}",
        source.streamed_on.strftime("%d %B %Y"),
        source.name
    );
    let pad = round(h * 0.008).max(5);
    let margin = round(w * 0.02).max(5);
    let box_width = round(font.width(&text) as f64) + 2 * pad;
    if box_width > width as i64 - 2 * margin {
        bail!("Source attribution is too wide; shorten its name.");
    }
    let left = width as i64 - margin - box_width;
    let top = round(h * 0.02);
    let mut image = RgbaImage::new(width, height);
    fill(
        &mut image,
        left,
        top,
        width as i64 - margin,
        top + round(h * 0.026) + 2 * pad,
        [12, 15, 18, 245],
    );
    font.draw(
        &mut image,
        (left + pad) as i32,
        (top + pad) as i32,
        [255, 255, 255],
        &text,
    );
    image
        .save(path)
        .with_context(|| format!("save {}", path.display()))?;
    Ok(())
}

/// Download an image (at most 10 MB) with its EXIF orientation applied.
pub async fn fetch_image(http: &reqwest::Client, url: &str) -> Result<DynamicImage> {
    if !url.starts_with("https://") {
        bail!("Images must use HTTPS URLs.");
    }
    let mut response = http.get(url).send().await?.error_for_status()?;
    let mut data = Vec::new();
    while let Some(chunk) = response.chunk().await? {
        data.extend_from_slice(&chunk);
        if data.len() > MAX_IMAGE_BYTES {
            bail!("The image exceeds 10 MB.");
        }
    }
    tokio::task::spawn_blocking(move || -> Result<DynamicImage> {
        let mut decoder = ImageReader::new(std::io::Cursor::new(data))
            .with_guessed_format()?
            .into_decoder()?;
        let orientation = decoder.orientation()?;
        let mut image = DynamicImage::from_decoder(decoder)?;
        image.apply_orientation(orientation);
        Ok(image)
    })
    .await?
}

/// Download a card image once and keep the exact reviewed asset, so retries
/// and upload recovery render the same pixels.
pub async fn image_asset(http: &reqwest::Client, url: &str, assets: &Path) -> Result<DynamicImage> {
    use sha2::Digest;
    let path: PathBuf = assets.join(format!(
        "{}.png",
        hex::encode(sha2::Sha256::digest(url.as_bytes()))
    ));
    if !path.exists() {
        let image = fetch_image(http, url).await?;
        let target = path.clone();
        tokio::task::spawn_blocking(move || -> Result<()> {
            let mut image = DynamicImage::ImageRgb8(image.to_rgb8());
            if image.width() > 1920 || image.height() > 1920 {
                image = image.thumbnail(1920, 1920);
            }
            std::fs::create_dir_all(target.parent().context("assets folder")?)?;
            let partial = target.with_extension("partial.png");
            image.save(&partial)?;
            std::fs::rename(&partial, &target)?;
            Ok(())
        })
        .await??;
    }
    Ok(image::open(&path)?.to_rgb8().into())
}
