import os

# External tools
FFMPEG = os.environ.get("FFMPEG_PATH", "/opt/homebrew/bin/ffmpeg")

# AI Models
CLIP_MODEL = "moonshotai/kimi-k2.5"
IMAGE_MODEL = "google/gemini-2.5-flash-image"

# AssemblyAI Models
ASSEMBLYAI_SPEECH_MODELS = ["universal-2"]

# Timeouts
MAX_TRANSCRIPTION_WAIT_SECONDS = 1800  # 30 minutes
HTTP_TIMEOUT = 30  # seconds per HTTP request

# Thumbnail settings
ILLUSTRATION_SIZE = (500, 500)  # (width, height) to scale cropped illustration to
ILLUSTRATION_CENTER_X = 800    # x coordinate to center illustration on template
ILLUSTRATION_CENTER_Y = 250    # y coordinate to center illustration on template

# Prompts
CLIP_EXTRACTION_PROMPT_TEMPLATE = """
This is a transcript of a podcast conversation between the American video game designer and computer programmer
Jonathan Blow and a host.
Analyze this transcript and extract as many interesting clips as possible that would work well as long-form content on YouTube.
Do not have any overlap between clips.
Each clip should be focused on a single topic or theme. Prioritize moments that are engaging and insightful.
Each clip should be around 10 minutes long (with a minimum of 8 minutes and a maximum of 20 minutes).

Return JSON in this exact format:
{{
  "clips": [
    {{
      "title": "Jonathan Blow on [Short descriptive title for the clip]",
      "description": "Brief description for YouTube",
      "start_ms": 12345,
      "end_ms": 67890,
      "tags": ["tag1", "tag2"],
      "thumbnail_prompt": "Detailed prompt for generating a thumbnail image. Do not include instructions for the image generation, just a description of the content of the image. Do not include Jonathan Blow's name in the thumbnail prompt."
    }}
  ]
}}

IMPORTANT: The title must start with "Jonathan Blow on" and then a short descriptive title for the clip
"""

ILLUSTRATION_PROMPT_TEMPLATE = """
Create a simple illustration for a YouTube Video Thumbnail about: {prompt}.
Style: Clean, minimal, high contrast, suitable for overlay on a thumbnail.
The illustration should be a single cohesive visual element (icon, symbol, or simple graphic)
that represents the topic. Use colors, clear shapes, no faces.
IMPORTANT: Use a pure WHITE background (#FFFFFF) to match the thumbnail template.
"""
