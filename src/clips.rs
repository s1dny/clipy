//! Agent-authored clip selections: the render contract, independent of
//! editorial style or channel policy.

use anyhow::{Result, bail};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

pub const MIN_VIDEO_MS: i64 = 8 * 60 * 1000;
const EM_DASH: char = '\u{2014}';

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct SourceAttribution {
    /// The source's name as shown on screen (at most 80 characters).
    pub name: String,
    /// The verified stream or recording date (YYYY-MM-DD), never an upload date.
    #[schemars(with = "String")]
    pub streamed_on: jiff::civil::Date,
    /// How the date was verified.
    pub evidence: String,
}

/// A retained source range, in source milliseconds.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct SourceRange {
    pub start_ms: i64,
    pub end_ms: i64,
    pub reason: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Default, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum Position {
    TopLeft,
    TopRight,
    #[default]
    BottomLeft,
    BottomRight,
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize, JsonSchema)]
pub enum Label {
    Context,
    Recorded,
    Uploaded,
    Question,
    Reference,
}

/// A brief card, timed against the FINAL edited video, in milliseconds. The
/// label is metadata and is never drawn.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Annotation {
    pub start_ms: i64,
    pub end_ms: i64,
    pub label: Label,
    /// Up to 700 characters of real background.
    pub text: String,
    pub evidence: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub heading: Option<String>,
    /// An HTTPS image shown beside the text.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub image_url: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub image_credit: Option<String>,
    #[serde(default)]
    pub position: Position,
}

/// Hold an inspected source frame while the edited audio continues.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct VisualHold {
    /// FINAL output milliseconds.
    pub start_ms: i64,
    pub end_ms: i64,
    /// The SOURCE millisecond of the frame to hold.
    pub source_ms: i64,
    pub reason: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Chapter {
    /// Seconds into the FINAL clip.
    pub start_seconds: i64,
    pub title: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct ClipSelection {
    pub title: String,
    /// Your accurate summary of the argument.
    pub description: String,
    /// Source-relative start of the excerpt.
    pub start_ms: i64,
    /// Source-relative end of the excerpt.
    pub end_ms: i64,
    /// A visual description of the clip's idea for the illustration.
    pub thumbnail_prompt: String,
    #[serde(default)]
    pub chapters: Vec<Chapter>,
    #[serde(default)]
    pub score: Option<f64>,
    /// Chronological retained SOURCE ranges within start_ms..end_ms; empty keeps the whole excerpt.
    #[serde(default)]
    pub keep: Vec<SourceRange>,
    #[serde(default)]
    pub annotations: Vec<Annotation>,
    #[serde(default)]
    pub visual_holds: Vec<VisualHold>,
    #[serde(default)]
    pub source: Option<SourceAttribution>,
}

fn no_em_dash(text: &str, what: &str) -> Result<()> {
    if text.contains(EM_DASH) {
        bail!("Em dashes are not allowed in {what}.");
    }
    Ok(())
}

fn length(text: &str, min: usize, max: usize, what: &str) -> Result<()> {
    let n = text.chars().count();
    if n < min || n > max {
        bail!("{what} must be {min} to {max} characters.");
    }
    Ok(())
}

impl ClipSelection {
    /// Output length after the keep ranges.
    pub fn duration_ms(&self) -> i64 {
        if self.keep.is_empty() {
            self.end_ms - self.start_ms
        } else {
            self.keep.iter().map(|r| r.end_ms - r.start_ms).sum()
        }
    }

    /// The render contract: ordered, nonoverlapping, inside their intervals.
    pub fn validate(&self) -> Result<()> {
        length(&self.title, 1, usize::MAX, "title")?;
        length(&self.description, 1, usize::MAX, "description")?;
        length(&self.thumbnail_prompt, 1, usize::MAX, "thumbnail_prompt")?;
        no_em_dash(&self.title, "video text")?;
        no_em_dash(&self.description, "video text")?;
        if self.start_ms < 0 || self.end_ms <= self.start_ms {
            bail!("end_ms must follow start_ms, and start_ms must be nonnegative.");
        }
        if let Some(source) = &self.source {
            length(&source.name, 1, 80, "source name")?;
            length(&source.evidence, 1, usize::MAX, "source evidence")?;
            no_em_dash(&source.name, "the source name")?;
        }
        let mut previous = self.start_ms;
        for span in &self.keep {
            length(&span.reason, 1, usize::MAX, "keep reason")?;
            if !(previous <= span.start_ms
                && span.start_ms < span.end_ms
                && span.end_ms <= self.end_ms)
            {
                bail!(
                    "keep ranges must be chronological, nonoverlapping and inside the source interval."
                );
            }
            previous = span.end_ms;
        }
        let duration = self.duration_ms();
        let mut previous = 0;
        for card in &self.annotations {
            length(&card.text, 1, 700, "annotation text")?;
            length(&card.evidence, 1, usize::MAX, "annotation evidence")?;
            no_em_dash(&card.text, "on-screen text")?;
            if let Some(heading) = &card.heading {
                length(heading, 1, 80, "annotation heading")?;
                no_em_dash(heading, "on-screen text")?;
            }
            if let Some(url) = &card.image_url {
                if !url.starts_with("https://") {
                    bail!("Card images must use HTTPS URLs.");
                }
                match &card.image_credit {
                    Some(credit) => length(credit, 1, 500, "image_credit")?,
                    None => bail!("An image needs its source/credit."),
                }
            }
            if !(previous <= card.start_ms
                && card.start_ms < card.end_ms
                && card.end_ms <= duration)
            {
                bail!(
                    "Annotations must be chronological, nonoverlapping and inside the edited output."
                );
            }
            previous = card.end_ms;
        }
        let mut previous = 0;
        for hold in &self.visual_holds {
            length(&hold.reason, 1, usize::MAX, "visual hold reason")?;
            if !(previous <= hold.start_ms
                && hold.start_ms < hold.end_ms
                && hold.end_ms <= duration)
            {
                bail!(
                    "Visual holds must be chronological, nonoverlapping and inside the edited output."
                );
            }
            if !(self.start_ms <= hold.source_ms && hold.source_ms < self.end_ms) {
                bail!("Held frames must come from the selected source interval.");
            }
            previous = hold.end_ms;
        }
        for chapter in &self.chapters {
            length(&chapter.title, 1, usize::MAX, "chapter title")?;
            no_em_dash(&chapter.title, "chapter titles")?;
            if chapter.start_seconds < 0 || chapter.start_seconds * 1000 >= duration {
                bail!("A chapter extends beyond its clip.");
            }
        }
        Ok(())
    }

    /// What every published video additionally needs.
    pub fn validate_release(&self) -> Result<()> {
        self.validate()?;
        if self.duration_ms() < MIN_VIDEO_MS {
            bail!(
                "Every video must be at least 8 minutes AFTER all edits. Keep worthwhile asides; do not pad a short clip."
            );
        }
        if self.source.is_none() {
            bail!(
                "Every video needs source name, verified streamed_on date, and date evidence. An upload date is not a stream date."
            );
        }
        Ok(())
    }
}

fn clock(seconds: i64) -> String {
    let (minutes, secs) = (seconds / 60, seconds % 60);
    let (hours, minutes) = (minutes / 60, minutes % 60);
    if hours > 0 {
        format!("{hours}:{minutes:02}:{secs:02}")
    } else {
        format!("{minutes}:{secs:02}")
    }
}

pub fn chapters_text(chapters: &[Chapter]) -> String {
    chapters
        .iter()
        .map(|c| format!("{} {}", clock(c.start_seconds), c.title))
        .collect::<Vec<_>>()
        .join("\n")
}
