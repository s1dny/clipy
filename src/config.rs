//! config.toml: models, ffmpeg, and each channel's settings and prompts.
//!
//! A channel's settings are [defaults], then [channels.<name>] on top, then
//! the agent's prompt overrides (stored in the database).

use std::collections::BTreeMap;
use std::path::Path;

use anyhow::{Context, Result, bail};
use serde::Deserialize;

pub const PROMPT_KEYS: [&str; 3] = [
    "editorial_guidance",
    "illustration_prompt_template",
    "description_template",
];

#[derive(Debug, Clone, Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct Runtime {
    ffmpeg_path: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct Models {
    image: Option<String>,
    speech: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Default)]
#[serde(deny_unknown_fields)]
pub struct ChannelTable {
    handle: Option<String>,
    speaker: Option<String>,
    thumbnail_template: Option<String>,
    category_id: Option<String>,
    policy: Option<String>,
    editorial_guidance: Option<String>,
    illustration_prompt_template: Option<String>,
    description_template: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct File {
    #[serde(default)]
    runtime: Runtime,
    #[serde(default)]
    models: Models,
    #[serde(default)]
    defaults: ChannelTable,
    #[serde(default)]
    channels: toml::Table,
}

#[derive(Debug, Clone)]
pub struct ChannelConfig {
    /// The YouTube handle ("@ExampleClips") its token must belong to.
    pub handle: String,
    /// Every source's main speaker, for a channel about one person.
    pub speaker: Option<String>,
    /// A fixed thumbnail in <data dir>/thumbnails/; without one, thumbnails
    /// put the speaker's headshot on the left.
    pub thumbnail_template: Option<String>,
    pub category_id: String,
    /// The channel's part of the agent's instructions, after agent/prompt.md.
    pub policy: String,
    pub editorial_guidance: String,
    pub illustration_prompt_template: String,
    pub description_template: String,
}

impl ChannelConfig {
    pub fn prompt(&self, key: &str) -> &str {
        match key {
            "editorial_guidance" => &self.editorial_guidance,
            "illustration_prompt_template" => &self.illustration_prompt_template,
            "description_template" => &self.description_template,
            _ => unreachable!("unknown prompt {key}"),
        }
    }
}

#[derive(Debug, Clone)]
pub struct Config {
    pub ffmpeg: String,
    pub image_model: String,
    pub speech_model: String,
    /// Channels in config.toml order.
    pub channels: Vec<(String, ChannelTable)>,
}

impl Config {
    pub fn load(path: &Path) -> Result<Self> {
        let text = std::fs::read_to_string(path)
            .with_context(|| format!("Configuration file not found: {}", path.display()))?;
        let file: File =
            toml::from_str(&text).with_context(|| format!("Invalid TOML in {}", path.display()))?;
        let mut channels = Vec::new();
        for (name, value) in file.channels {
            let table: ChannelTable = value
                .try_into()
                .with_context(|| format!("Invalid [channels.{name}] in config.toml"))?;
            let d = &file.defaults;
            let merged = ChannelTable {
                handle: table.handle.or(d.handle.clone()),
                speaker: table.speaker.or(d.speaker.clone()),
                thumbnail_template: table.thumbnail_template.or(d.thumbnail_template.clone()),
                category_id: table.category_id.or(d.category_id.clone()),
                policy: table.policy.or(d.policy.clone()),
                editorial_guidance: table.editorial_guidance.or(d.editorial_guidance.clone()),
                illustration_prompt_template: table
                    .illustration_prompt_template
                    .or(d.illustration_prompt_template.clone()),
                description_template: table
                    .description_template
                    .or(d.description_template.clone()),
            };
            channels.push((name, merged));
        }
        let config = Self {
            ffmpeg: file.runtime.ffmpeg_path.unwrap_or_else(|| "ffmpeg".into()),
            image_model: file
                .models
                .image
                .unwrap_or_else(|| "bytedance-seed/seedream-5-0-lite".into()),
            speech_model: file
                .models
                .speech
                .unwrap_or_else(|| "universal-3-5-pro".into()),
            channels,
        };
        for (name, _) in &config.channels {
            config.channel(name, &BTreeMap::new())?;
        }
        Ok(config)
    }

    pub fn names(&self) -> Vec<String> {
        self.channels.iter().map(|(name, _)| name.clone()).collect()
    }

    pub fn has(&self, name: &str) -> bool {
        self.channels.iter().any(|(n, _)| n == name)
    }

    /// The channel's settings with the agent's prompt overrides applied.
    pub fn channel(
        &self,
        name: &str,
        overrides: &BTreeMap<String, String>,
    ) -> Result<ChannelConfig> {
        let Some((_, table)) = self.channels.iter().find(|(n, _)| n == name) else {
            bail!("config.toml has no [channels.{name}]");
        };
        let prompt = |key: &str, value: &Option<String>| -> Result<String> {
            overrides
                .get(key)
                .cloned()
                .or_else(|| value.clone())
                .with_context(|| format!("Missing config.toml field '{key}' for channel {name}"))
        };
        Ok(ChannelConfig {
            handle: table.handle.clone().with_context(|| {
                format!("Missing config.toml field 'handle' for channel {name}")
            })?,
            speaker: table.speaker.clone(),
            thumbnail_template: table.thumbnail_template.clone(),
            category_id: table.category_id.clone().unwrap_or_else(|| "22".into()),
            policy: table.policy.clone().unwrap_or_default(),
            editorial_guidance: prompt("editorial_guidance", &table.editorial_guidance)?,
            illustration_prompt_template: prompt(
                "illustration_prompt_template",
                &table.illustration_prompt_template,
            )?,
            description_template: prompt("description_template", &table.description_template)?,
        })
    }
}
