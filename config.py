import os

# External tools
FFMPEG = os.environ.get("FFMPEG_PATH", "/opt/homebrew/bin/ffmpeg")

# AI Models
CLIP_MODEL = "google/gemini-3-pro-preview"
IMAGE_MODEL = "black-forest-labs/flux.2-pro"

# API
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

# AssemblyAI Models
ASSEMBLYAI_SPEECH_MODELS = ["universal-3"]

# Timeouts
MAX_TRANSCRIPTION_WAIT_SECONDS = 3600  # 60 minutes
HTTP_TIMEOUT = 30  # seconds per HTTP request
CLIP_EXTRACTION_TIMEOUT = 3600  # seconds, LLM clip extraction (large context)
IMAGE_GENERATION_TIMEOUT = 120  # seconds, image generation per request

# Thumbnail settings
ILLUSTRATION_SIZE = (500, 500)  # (width, height) to scale cropped illustration to
ILLUSTRATION_CENTER_X = 850    # x coordinate to center illustration on template
ILLUSTRATION_CENTER_Y = 350    # y coordinate to center illustration on template

# Prompts
CLIP_EXTRACTION_PROMPT_TEMPLATE = """
This is a transcript of a podcast conversation between the American video game designer and computer programmer
Jonathan Blow and a host.
Analyze this ENTIRE transcript and extract as many consecutive clip segments as possible that would work well as long-form content on YouTube.
Make sure each clip starts at an understandable point in the conversation, such as the beginning of a new topic or a natural pause.
Do not ever cut a clip in the middle of a sentence or thought.
Always end a clip at a natural stopping point.
Each clip should be focused on a single topic or theme.
Each clip can be roughly around 10 minutes long, a clip can be longer to let a speaker finish their thought (STRICT MINIMUM of 8 minutes and STRICT MAXIMUM of 20 minutes).

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
The illustration should be a single cohesive visual element (icon, symbol, or simple graphic) that represents the topic. 
Use colors, clear shapes, no faces or long sentences of words.
Do not cut off the illustration, it should be fully contained within the canvas.
IMPORTANT: Use a pure WHITE background (#FFFFFF) to match the thumbnail template.
"""
