"""Standalone Chess Companion - Simplified current board analysis

Simplified from TV companion architecture for chess-specific analysis:
- Board change detection via consensus vision
- Current position analysis (no complex queuing)
- Vector search through historical games database
- Expert chess commentary via Gemini Live
"""

import argparse
import asyncio
import base64
from datetime import datetime
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback
from typing import Dict, List, Optional

import chess
import chess.engine
# Import chess-specific components  
from chess_analyzer import ChessAnalyzer
from roboflow import roboflow_piece_detection as consensus_piece_detection
import cv2
from scenedetection import ChessSceneDetector
from google import genai
from google.cloud import speech
from google.genai import types
from mem0 import MemoryClient
import numpy as np
from PIL import Image
import pyaudio
from stockfish_pool import StockfishEnginePool, create_quick_analysis_pool
from vector_search import ChessVectorSearch, SearchResult


def parse_args():
  parser = argparse.ArgumentParser(
      description="Chess Companion with simplified board analysis"
  )
  parser.add_argument(
      "--debug",
      action="store_true",
      help="Save debug images (HDMI captures and crops)",
  )
  parser.add_argument(
      "--no-watch",
      action="store_true",
      help="Start with watching mode OFF (manual queries only)",
  )
  return parser.parse_args()


if sys.version_info < (3, 11, 0):
  import exceptiongroup
  import taskgroup

  asyncio.TaskGroup = taskgroup.TaskGroup
  asyncio.ExceptionGroup = exceptiongroup.ExceptionGroup

# Audio settings
FORMAT = pyaudio.paInt16
CHANNELS = 1
SEND_SAMPLE_RATE = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE = 1024

# HDMI capture device settings
HDMI_VIDEO_DEVICE = "/dev/video11"
HDMI_AUDIO_TARGET = (
    "alsa_input.usb-MACROSILICON_USB3.0_Video_26241327-02.analog-stereo"
)

MODEL = "gemini-2.5-flash-preview-native-audio-dialog"

# Chess companion configuration
CHESS_CONFIG = {
    "response_modalities": ["AUDIO"],
    "system_instruction": (
        """You are an expert chess companion with deep knowledge of chess theory, tactics, strategy, and chess history.

You can provide insightful commentary AND control the TV for chess content.

## IMPORTANT: When to Analyze the Current Position
ALWAYS use the `analyze_current_position` tool when users ask about:
- What should [player] do/play? (e.g. "What should Magnus do?" "What should Alireza play?")
- Current position evaluation (e.g. "How good is this position?" "Who's winning?") 
- Best moves or move suggestions (e.g. "What's the best move?" "Any good moves here?")
- Position-specific questions (e.g. "Is this winning?" "Should White attack?")

Don't give generic chess advice - analyze the actual board position first!

## TV Control Capabilities:
- Search for and play chess videos using search_and_play_content  
- Pause playback with pause_playback
- Access user's viewing history with search_user_history
- Toggle watching mode on/off for automatic position commentary

## Chess Commentary Style:
When you receive position analysis packages (from analyze_current_position), provide expert chess commentary that enhances understanding:

Be analytical and educational:
- Explain tactical themes, strategic concepts, and positional ideas
- Compare with similar master games from the historical database
- Point out key moves, blunders, and brilliant combinations  
- Discuss opening theory, middlegame plans, and endgame technique
- Analyze piece activity, king safety, and pawn structure
- Share engine evaluations and best move suggestions
- Ask thought-provoking questions about player intentions
- Connect current position to chess history and famous games

Strike a balance between being informative and conversational - like watching with a chess master who notices details others might miss.

Feel free to suggest chess content to watch or help users find specific games.
"""
    ),
    "tools": [{
        "function_declarations": [
            {
                "name": "analyze_current_position",
                "description": (
                    "Take a fresh screenshot and provide comprehensive analysis"
                    " of the current chess position. Includes engine"
                    " evaluation, strategic themes, similar master games, and"
                    " expert commentary. Use this when user asks questions"
                    " about the current board state."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Optional: User's specific question about the"
                                " position (e.g., 'What should White play?',"
                                " 'Is this winning?', 'Evaluate this position')"
                            ),
                        }
                    },
                    "required": [],
                },
            },
            {
                "name": "search_and_play_content",
                "description": (
                    "Search for and start playing chess content on the TV using"
                    " Google TV's universal search. Works well with queries"
                    " like 'magnus carlsen vs hikaru nakamura' or 'world"
                    " championship 2023'"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": (
                                "Chess game, tournament, or players to"
                                " search for"
                            ),
                        }
                    },
                    "required": ["title"],
                },
            },
            {
                "name": "search_user_history",
                "description": (
                    "Search the user's personal viewing history and past chess"
                    " discussions. Use this to recall previous games watched,"
                    " positions analyzed, or questions asked."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Optional: What to search for in chess history"
                                " (e.g., 'Kasparov games', 'tactical puzzles',"
                                " 'endgame positions'). Leave blank to get"
                                " recent activity."
                            ),
                        }
                    },
                    "required": [],
                },
            },
            {
                "name": "start_watching_mode",
                "description": (
                    "Start automatically commenting on positions as they change"
                    " during live games"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "stop_watching_mode",
                "description": "Stop automatic position commentary",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "pause_playback",
                "description": (
                    "Pause the currently playing chess content on TV"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        ]
    }],
}


class TVAudioStream:
  """Captures TV audio using pw-cat and provides it as a stream for transcription"""

  def __init__(self):
    import queue

    self._buff = (
        queue.Queue()
    )  # Use sync queue for Google Cloud Speech compatibility
    self.closed = True
    self.audio_process = None

  async def __aenter__(self):
    self.closed = False
    # Start pw-cat process for TV audio
    cmd = [
        "pw-cat",
        "--record",
        "-",
        "--target",
        HDMI_AUDIO_TARGET,
        "--rate",
        str(SEND_SAMPLE_RATE),
        "--channels",
        "1",
        "--format",
        "s16",
        "--raw",
    ]

    self.audio_process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    # Start feeding audio data into buffer
    asyncio.create_task(self._feed_buffer())
    return self

  async def __aexit__(self, type, value, traceback):
    self.closed = True
    if self.audio_process:
      self.audio_process.terminate()
      await self.audio_process.wait()
    self._buff.put(None)  # Signal generator to terminate (sync put)

  async def _feed_buffer(self):
    """Read audio from pw-cat and put into buffer"""
    chunk_size = int(SEND_SAMPLE_RATE / 10)  # 100ms chunks
    bytes_expected = chunk_size * 2  # 2 bytes per s16 sample
    chunks_sent = 0

    while not self.closed:
      try:
        data = await self.audio_process.stdout.read(bytes_expected)
        if not data:
          print(f"📡 No more audio data from pw-cat")
          break

        self._buff.put(data)  # Sync put to sync queue
        chunks_sent += 1

        # Remove noisy logging - only log major milestones
        if chunks_sent % 500 == 0:  # Log every 50 seconds instead
          print(f"📡 Audio buffer: {chunks_sent} chunks processed")

      except Exception as e:
        print(f"❌ Audio feed error: {e}")
        break

  def generator(self):
    """Generator that yields audio chunks for Google Cloud Speech"""
    chunks_yielded = 0
    while not self.closed:
      try:
        chunk = self._buff.get(timeout=1.0)  # Sync get with timeout
        if chunk is None:
          print("📡 Audio generator: received termination signal")
          return

        chunks_yielded += 1
        # Remove noisy logging - only log major milestones
        if chunks_yielded % 500 == 0:  # Log every 50 seconds instead
          print(f"📡 Audio generator: {chunks_yielded} chunks yielded")

        yield chunk
      except:
        print("⚠️ Audio generator: timeout waiting for chunk")
        continue


class ChessCompanionSimplified:
  """Simplified Chess Companion - Current board analysis only"""

  def __init__(self, debug_mode=False, watching_mode=True):
    # Core state - just current board and analysis
    self.current_board = None
    self.current_analysis = None  # Current analysis with both perspectives
    self.analyzing = False
    self.commentary_buffer = []  # Recent commentary for current board

    # Watching mode - configurable
    self.watching_mode = watching_mode

    # Scene detection and bounding box caching
    self.current_board_mask = None  # Cached board bounding box
    self.scene_detector = None
    self.scene_detection_task = None
    self.board_mask_last_updated = None

    # Verify Roboflow API key for vision pipeline
    if not os.getenv("ROBOFLOW_API_KEY"):
      raise ValueError("ROBOFLOW_API_KEY environment variable required for vision pipeline")

    # Debug setup
    self.debug_mode = debug_mode
    if self.debug_mode:
      self.debug_dir = Path("debug_chess_frames")
      self.debug_dir.mkdir(exist_ok=True)
      print(f"🐛 Debug mode: saving frames to {self.debug_dir}")

    # Gemini client and session
    self.client = genai.Client()
    self.session = None
    self.audio_in_queue = None

    # Chess analysis components
    self.vector_search = ChessVectorSearch()
    self.engine_pool = create_quick_analysis_pool(pool_size=4)
    self.analyzer = ChessAnalyzer(self.vector_search, self.engine_pool, self.client)

    # Fresh analysis task tracking
    self.fresh_analysis_task = None
    self.pending_user_query = None

    # Audio components
    self.pya = None
    self.mic_stream = None
    self.shared_cap = None

    # Memory and speech
    self.memory_client = self._init_memory_client()
    self.speech_client = speech.SpeechClient()
    self.speech_config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=SEND_SAMPLE_RATE,
        language_code="en-US",
        max_alternatives=1,
    )
    self.streaming_config = speech.StreamingRecognitionConfig(
        config=self.speech_config, interim_results=True
    )

    print("♟️  Chess Companion (Simplified) initialized")
    print(f"👁️  Watching mode: {'ON' if self.watching_mode else 'OFF'}")
    print("📊 Vector search ready with chess games database")
    print("🔧 Stockfish engine pool ready for analysis")

  def _init_memory_client(self):
    """Initialize mem0 client for episodic memory"""
    api_key = os.getenv("MEM0_API_KEY")
    if not api_key:
      print("⚠️  MEM0_API_KEY not set, episodic memory disabled")
      return None

    try:
      client = MemoryClient(
          api_key=api_key,
          org_id="org_lOJM2vCRxHhS7myVb0vvaaY1rUauhqkKbg7Dg7KZ",
          project_id="proj_I6CXbVIrt0AFlWE0MU3TyKxkkYJam2bHm8nUxgEu",
      )
      print("✓ Episodic memory initialized")
      return client
    except Exception as e:
      print(f"⚠️  Failed to initialize memory: {e}")
      return None

  def find_pulse_device(self):
    """Find a PulseAudio device that works with PipeWire"""
    for i in range(self.pya.get_device_count()):
      info = self.pya.get_device_info_by_index(i)
      if "pulse" in info["name"].lower() and info["maxInputChannels"] > 0:
        return info
    return self.pya.get_default_input_device_info()

  async def send_text(self):
    """Allow user to send text messages"""
    while True:
      text = await asyncio.to_thread(input, "message > ")
      if text.lower() == "q":
        break
      await self.session.send_realtime_input(text=text or ".")

  def _convert_frame_to_base64(self, frame_img):
    """Convert OpenCV frame to base64 format for Gemini"""
    try:
      # Convert numpy array to PIL Image
      frame_rgb = cv2.cvtColor(frame_img, cv2.COLOR_BGR2RGB)
      img = Image.fromarray(frame_rgb)
      img.thumbnail([1024, 1024])

      image_io = io.BytesIO()
      img.save(image_io, format="jpeg")
      image_io.seek(0)

      image_bytes = image_io.read()
      print(f"🔧 Frame converted successfully: {len(image_bytes)} bytes")

      return {
          "mime_type": "image/jpeg",
          "data": base64.b64encode(image_bytes).decode(),
      }
    except Exception as e:
      print(f"❌ Frame conversion failed: {e}")
      raise

  async def detect_initial_board_mask(self):
    """Detect initial board location on startup - no scene change needed"""
    print("🎯 Detecting initial board location...")
    
    # Initialize video capture
    self.shared_cap = cv2.VideoCapture(HDMI_VIDEO_DEVICE)
    if not self.shared_cap.isOpened():
      print(f"❌ Cannot open HDMI video device {HDMI_VIDEO_DEVICE}")
      return

    self.shared_cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    self.shared_cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    print("✅ HDMI capture ready")
    
    ret, frame = self.shared_cap.read()
    if ret:
      await self.update_board_mask(frame)
      print("✅ Initial board mask detected")
    else:
      print("❌ Failed to capture initial frame for board detection")

  async def start_scene_detection(self):
    """Start background scene detection for board mask updates"""
    print("🎬 Starting background scene detection...")
    
    if not self.shared_cap or not self.shared_cap.isOpened():
      print("❌ Video capture not ready for scene detection")
      return
    
    # Initialize scene detector
    debug_dir = str(self.debug_dir) if self.debug_mode else None
    self.scene_detector = ChessSceneDetector(debug_dir=debug_dir)
    
    # Define callback for scene changes
    async def on_scene_change(frame):
      print("🎬 Scene change detected - queuing board mask update")
      asyncio.create_task(self.update_board_mask(frame))
    
    # Start detection
    await self.scene_detector.start_detection(self.shared_cap, on_scene_change)

  async def fast_fen_detection_loop(self):
    """Fast FEN detection using cached board mask"""
    print("♟️ Starting fast FEN detection loop...")
    
    detection_count = 0
    
    while True:
      try:
        if self.current_board_mask is None:
          print("⏳ Waiting for board mask...")
          await asyncio.sleep(2)
          continue
        
        detection_count += 1
        ret, frame = self.shared_cap.read()
        
        if not ret:
          print("⚠️  Failed to capture frame for FEN detection")
          await asyncio.sleep(2)
          continue

        # Use cached mask for fast FEN extraction
        new_fen = await self.extract_fen_with_cached_mask(frame)
        
        if new_fen and self.is_valid_fen(new_fen):
          if new_fen != self.current_board:
            print(f"🆕 Position change detected #{detection_count}: {new_fen[:30]}...")
            await self.on_board_change(new_fen, frame)
          else:
            if detection_count % 6 == 0:  # Log every 30 seconds (6 * 5s)
              print(f"📍 Position stable #{detection_count}: {new_fen[:30]}...")
        else:
          print(f"❌ Invalid/no FEN detected #{detection_count}")
        
        await asyncio.sleep(5)  # Check every 5 seconds - much faster than 30s
        
      except Exception as e:
        print(f"❌ Fast FEN detection error: {e}")
        traceback.print_exc()
        await asyncio.sleep(5)

  async def update_board_mask(self, frame):
    """Update cached board bounding box from scene change"""
    try:
      print("📐 Updating board bounding box...")
      start_time = time.time()
      
      # Step 1: Fresh screenshot → 1024x1024 (for segmentation)
      ret, screenshot = self.shared_cap.read()
      if not ret:
        print("❌ Failed to capture screenshot for board mask")
        return
      
      # Convert to PIL and resize to 1024x1024 
      screenshot_rgb = cv2.cvtColor(screenshot, cv2.COLOR_BGR2RGB)
      pil_screenshot = Image.fromarray(screenshot_rgb)
      pil_1024 = pil_screenshot.resize((1024, 1024), Image.LANCZOS)
      
      print(f"📐 Screenshot: {screenshot.shape[:2]} → 1024x1024 for segmentation")
      
      # Step 2: Segment to get bounding box (on 1024x1024 space)
      from roboflow import ChessVisionPipeline
      pipeline = ChessVisionPipeline(debug_dir=str(self.debug_dir) if self.debug_mode else None)
      
      segmentation_result = pipeline.segment_board_direct(pil_1024)
      
      if segmentation_result.get("predictions"):
        bbox = pipeline.extract_bbox_from_segmentation(segmentation_result)
        
        if bbox:
          # Cache bbox (coordinates in 1024x1024 space)
          self.current_board_mask = {
            "bbox": bbox["coords"],  # (x_min, y_min, x_max, y_max) in 1024x1024 space
            "confidence": bbox["confidence"], 
            "timestamp": start_time
          }
          print(f"✅ Board mask cached: {bbox['coords']}, confidence={bbox['confidence']:.3f}")
        else:
          print("❌ No valid bounding box extracted")
          self.current_board_mask = None
      else:
        print("❌ Board segmentation found no predictions")
        self.current_board_mask = None
      
      elapsed = time.time() - start_time
      print(f"✅ Board mask update complete in {elapsed:.1f}s")
      
    except Exception as e:
      print(f"❌ Board mask update failed: {e}")
      traceback.print_exc()
      self.current_board_mask = None

  async def extract_fen_with_cached_mask(self, frame):
    """Fast FEN extraction using cached board bounding box"""
    try:
      if not self.current_board_mask or "bbox" not in self.current_board_mask:
        print("⚠️  No cached board mask - falling back")
        return await self._fallback_full_detection(frame)
      
      # Step 1: Screenshot → 1024x1024 (same space as cached bbox)
      screenshot_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
      pil_screenshot = Image.fromarray(screenshot_rgb)
      pil_1024 = pil_screenshot.resize((1024, 1024), Image.LANCZOS)
      
      # Step 2: Crop using cached bbox (in 1024x1024 space)
      bbox = self.current_board_mask["bbox"]  # (x_min, y_min, x_max, y_max)
      pil_crop = pil_1024.crop(bbox)
      
      if pil_crop.size[0] == 0 or pil_crop.size[1] == 0:
        print("❌ Empty crop from cached bbox - falling back")
        return await self._fallback_full_detection(frame)
      
      # Step 3: Resize crop to 640x640 (optimal for piece detection)
      pil_crop_640 = pil_crop.resize((640, 640), Image.LANCZOS)
      
      print(f"🚀 Fast path: {frame.shape[:2]} → 1024² → crop → 640² → piece detection")
      
      # Step 4: Piece detection directly on PIL Image (ONE API CALL)
      from roboflow import ChessVisionPipeline
      pipeline = ChessVisionPipeline(debug_dir=str(self.debug_dir) if self.debug_mode else None)
      
      piece_result = pipeline.detect_pieces_direct(pil_crop_640, model_id="chess.comdetection/4")
      
      if not piece_result:
        print("❌ Fast piece detection failed - falling back")
        return await self._fallback_full_detection(frame)
      
      # Step 5: Convert to FEN (coordinate math only, no API calls)
      piece_count = len(piece_result.get("predictions", []))
      if piece_count < 2:
        print(f"❌ Fast path: Only {piece_count} pieces detected")
        return None
        
      fen, _ = pipeline.pieces_to_fen_from_dimensions(
        piece_result, 
        pil_crop_640.size[0], 
        pil_crop_640.size[1], 
        "chess.comdetection/4"
      )
      
      if fen == "8/8/8/8/8/8/8/8":
        print(f"❌ Empty board FEN from {piece_count} pieces")
        return None
      
      # Optional: Save debug frames
      if self.debug_mode:
        timestamp = datetime.now().strftime("%H%M%S_%f")[:-3]
        debug_path = self.debug_dir / f"fast_crop_{timestamp}.png"
        pil_crop_640.save(debug_path)
        print(f"🐛 Debug: Saved fast crop to {debug_path}")
      
      print(f"🚀 Fast FEN: {piece_count} pieces → {fen[:20]}...")
      return fen
      
    except Exception as e:
      print(f"❌ Fast FEN extraction failed: {e}")
      return await self._fallback_full_detection(frame)

  async def _fallback_full_detection(self, frame):
    """Fallback to full consensus detection when cached approach fails"""
    print("🔄 Fallback: Running full consensus detection...")
    try:
      temp_path = await self.save_frame_to_temp(frame)
      
      result = await consensus_piece_detection(
          temp_path,
          n=7,
          min_consensus=3,
          debug_dir=str(self.debug_dir) if self.debug_mode else None,
      )
      
      piece_count = result.get("piece_count", 0)
      if piece_count < 2:
        print(f"❌ Fallback: Only {piece_count} pieces detected")
        os.unlink(temp_path)
        return None
      
      new_fen = result["consensus_fen"]
      os.unlink(temp_path)
      
      if new_fen == "8/8/8/8/8/8/8/8":
        print(f"❌ Fallback: Empty board FEN despite {piece_count} pieces")
        return None
      
      print(f"🔄 Fallback detection complete: {new_fen[:20]}...")
      return new_fen
      
    except Exception as e:
      print(f"❌ Fallback detection failed: {e}")
      return None

  async def on_board_change(self, new_fen: str, frame):
    """Handle new board position detected"""
    print(f"🎯 Board changed from {self.current_board} to {new_fen}")

    # Show visual representation of the new position
    self._show_board_visualization(new_fen)

    # Update current state - keep commentary buffer for continuous narrative
    self.current_board = new_fen
    # Don't clear commentary_buffer - commentary flows across positions

    # Start analysis (don't wait for it)
    if not self.analyzing:
      asyncio.create_task(self.analyze_new_position(new_fen, frame))

  async def analyze_new_position(self, fen: str, frame=None):
    """Analyze new position using both perspectives"""
    if self.analyzing:
      return

    self.analyzing = True
    try:
      print(f"🧠 Analyzing new position with both perspectives: {fen[:30]}...")
      
      # Use analyzer to get both perspectives
      analyses = await self.analyzer.analyze_both_perspectives(
          fen=fen,
          frame=frame,
          commentary_context=self.commentary_buffer
      )
      
      # Update current analysis (overwrite)
      self.current_analysis = analyses
      print(f"✅ Current analysis updated for {fen[:30]}...")
      
      # Auto-send if watching mode (default to white perspective)
      if self.watching_mode:
        await self.send_analysis_to_gemini(analyses["white"])
        
    except Exception as e:
      print(f"❌ Analysis error: {e}")
      traceback.print_exc()
    finally:
      self.analyzing = False

  async def send_analysis_to_gemini(self, analysis):
    """Send analysis to Gemini Live for commentary"""
    try:
      # Use pre-formatted analysis text
      analysis_text = analysis.get("formatted_for_gemini", "Analysis unavailable")

      # Show what we're sending to Gemini
      print(f"\n📤 SENDING TO GEMINI:")
      print("=" * 50)
      print(analysis_text)
      print("=" * 50)

      # Build content parts - always include text analysis
      parts = [{"text": analysis_text}]

      # Add screenshot if available
      screenshot = analysis.get("board_screenshot")
      if screenshot:
        parts.append({
            "inline_data": {
                "mime_type": screenshot["mime_type"],
                "data": screenshot["data"],
            }
        })
        print(f"📸 Including screenshot with analysis")

      content = {"role": "user", "parts": parts}
      await self.session.send_client_content(turns=content, turn_complete=True)
      print(f"✅ Sent analysis to Gemini (watching mode) - {len(parts)} parts")

    except Exception as e:
      print(f"❌ Failed to send analysis: {e}")


  def is_valid_fen(self, fen: str) -> bool:
    """Check if a string is a valid FEN"""
    try:
      chess.Board(fen)
      return True
    except (ValueError, TypeError):
      return False

  def _show_board_visualization(self, fen: str):
    """Show visual 8x8 board representation"""
    try:
      board = chess.Board(f"{fen} w KQkq - 0 1")
      
      print(f"📋 8x8 BOARD VISUALIZATION:")
      print("   a b c d e f g h")
      for rank in range(8, 0, -1):  # 8 down to 1
        pieces_row = []
        for file in range(8):  # a to h
          square = chess.square(file, rank - 1)
          piece = board.piece_at(square)
          if piece:
            pieces_row.append(piece.symbol())
          else:
            pieces_row.append(".")
        print(f"{rank}: {' '.join(pieces_row)}")
    except Exception as e:
      print(f"⚠️ Board visualization failed: {e}")

  async def save_frame_to_temp(self, frame) -> str:
    """Save video frame to temporary file for vision analysis"""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
      temp_path = temp_file.name

    # Convert frame and save - normalize before vision analysis
    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    img = Image.fromarray(frame_rgb)
    img.thumbnail(
        (1024, 1024), Image.LANCZOS
    )  # Normalize for better vision results
    img.save(temp_path)

    return temp_path




  async def listen_user_audio(self):
    """Capture audio from user microphone for questions"""
    try:
      self.pya = pyaudio.PyAudio()
      mic_info = self.find_pulse_device()
      print(f"🎤 Using user microphone: {mic_info['name']}")

      self.mic_stream = await asyncio.to_thread(
          self.pya.open,
          format=FORMAT,
          channels=CHANNELS,
          rate=SEND_SAMPLE_RATE,
          input=True,
          input_device_index=mic_info["index"],
          frames_per_buffer=CHUNK_SIZE,
      )

      if __debug__:
        kwargs = {"exception_on_overflow": False}
      else:
        kwargs = {}

      while True:
        data = await asyncio.to_thread(
            self.mic_stream.read, CHUNK_SIZE, **kwargs
        )
        # Send directly to Gemini Live for user questions
        await self.session.send_realtime_input(
            audio={"data": data, "mime_type": "audio/pcm"}
        )

    except Exception as e:
      print(f"❌ User audio capture error: {e}")
      raise

  async def transcribe_tv_audio(self):
    """Continuously transcribe TV audio and add to commentary buffer"""
    print("🎤 Starting TV audio transcription...")

    while True:
      try:
        print("🎤 Creating new audio stream...")
        async with TVAudioStream() as stream:
          print("🎤 Audio stream created, starting transcription...")

          # Run transcription in a separate thread to avoid blocking
          await asyncio.to_thread(self._run_transcription_sync, stream)

      except Exception as e:
        print(f"❌ Transcription error: {e}")
        traceback.print_exc()
        await asyncio.sleep(2)  # Brief pause before restarting

  def _run_transcription_sync(self, stream):
    """Run transcription synchronously in a thread"""
    audio_generator = stream.generator()
    requests = (
        speech.StreamingRecognizeRequest(audio_content=content)
        for content in audio_generator
    )

    print("🎤 Sending requests to Google Cloud Speech...")
    responses = self.speech_client.streaming_recognize(
        self.streaming_config, requests
    )

    print("🎤 Processing responses...")
    transcripts_received = 0

    for response in responses:
      if not response.results:
        continue

      result = response.results[0]
      if not result.alternatives:
        continue

      transcript = result.alternatives[0].transcript

      # Only process final results to avoid spam
      if result.is_final and transcript.strip():
        transcripts_received += 1
        print(f"📝 Transcribed #{transcripts_received}: {transcript}")

        # Add to commentary buffer (for current board context)
        self.commentary_buffer.append(transcript)
        # Keep only last 10 commentary lines
        if len(self.commentary_buffer) > 10:
          self.commentary_buffer = self.commentary_buffer[-10:]

        print(
            f"📝 Commentary buffer now has {len(self.commentary_buffer)} items"
        )
        print(f"📝 Latest: {transcript}")

  async def receive_audio(self):
    """Receive Gemini's audio responses and handle tool calls"""
    while True:
      turn = self.session.receive()
      async for response in turn:
        # Audio data - queue immediately
        if data := response.data:
          await self.audio_in_queue.put(data)
          continue

        # Text response
        if text := response.text:
          print(f"♟️  Chess Companion: {text}")
          continue

        # Tool calls
        if response.tool_call:
          await self.handle_tool_call(response.tool_call)
          continue

      # Handle interruptions by clearing audio queue
      while not self.audio_in_queue.empty():
        try:
          self.audio_in_queue.get_nowait()
        except:
          break

  async def handle_tool_call(self, tool_call):
    """Handle tool calls - simplified for current board analysis"""
    function_responses = []

    for fc in tool_call.function_calls:
      print(f"🔧 Tool call: {fc.name}")

      if fc.name == "analyze_current_position":
        user_query = fc.args.get("query", "Analyze the current chess position")
        print(f"🔧 Current position analysis requested: '{user_query}'")

        # Simple: just use current analysis 
        if self.current_analysis:
          print(f"✅ Using current analysis (might be slightly stale)")
          
          # Determine perspective (default to white if can't determine)
          try:
            ret, frame = self.shared_cap.read()
            if ret:
              broadcast_context = await self.analyzer._extract_broadcast_context(frame)
              color = await self.analyzer.determine_query_perspective(user_query, broadcast_context)
            else:
              color = "white"
          except:
            color = "white"
          
          # Use the requested perspective, or fall back to white
          analysis = self.current_analysis.get(color, self.current_analysis.get("white", {}))
          
          if analysis:
            print(f"🎯 Using {color} perspective")
            
            # Update user query context
            analysis = analysis.copy()
            analysis["user_query"] = user_query
            analysis["formatted_for_gemini"] = self.analyzer._format_for_live_model(analysis)
            
            # Return analysis directly from tool (don't send separately)
            result = {
                "status": "analysis_ready",
                "analysis": analysis.get("formatted_for_gemini", "Analysis unavailable"),
                "query": user_query,
                "perspective": color
            }
            
            print(f"\n🔧 TOOL RESPONSE DEBUG:")
            print("=" * 60)
            print(f"Status: {result['status']}")
            print(f"Query: {result['query']}")
            print(f"Perspective: {result['perspective']}")
            print(f"Analysis length: {len(result.get('analysis', ''))}")
            print("\nFULL ANALYSIS CONTENT:")
            print("-" * 40)
            print(result.get('analysis', 'No analysis content'))
            print("-" * 40)
            print("=" * 60)
          else:
            result = {
                "status": "no_analysis",
                "message": "No analysis available yet. Please wait for position detection.",
            }
        else:
          # No current analysis available
          print(f"📍 No current analysis available")
          result = {
              "status": "no_analysis",
              "message": "No analysis available yet. Please wait for position detection.",
          }

      elif fc.name == "start_watching_mode":
        self.watching_mode = True
        result = {"status": "watching_mode_started"}
        print("👁️ Watching mode ON")

      elif fc.name == "stop_watching_mode":
        self.watching_mode = False
        result = {"status": "watching_mode_stopped"}
        print("👁️ Watching mode OFF")

      else:
        result = {"error": f"Unknown function: {fc.name}"}

      function_responses.append(
          types.FunctionResponse(id=fc.id, name=fc.name, response=result)
      )

    if function_responses:
      print(f"\n🔧 SENDING {len(function_responses)} TOOL RESPONSES TO GEMINI")
      for i, response in enumerate(function_responses):
        print(f"Tool Response {i+1}: {response.name} (ID: {response.id})")
        if hasattr(response.response, 'get') and 'analysis' in response.response:
          analysis_len = len(response.response.get('analysis', ''))
          print(f"  → Analysis content length: {analysis_len} chars")
        print(f"  → Full response: {response.response}")
      
      await self.session.send_tool_response(
          function_responses=function_responses
      )
      print("✅ Tool responses sent to Gemini successfully")

  async def play_audio(self):
    """Play Gemini's audio responses using pw-cat with pre-buffering"""
    # Wait for initial buffer
    initial_chunks = []
    for _ in range(3):  # Buffer 3 chunks before starting
      chunk = await self.audio_in_queue.get()
      initial_chunks.append(chunk)

    cmd = [
        "pw-cat",
        "--playback",
        "-",
        "--rate",
        str(RECEIVE_SAMPLE_RATE),
        "--channels",
        "1",
        "--format",
        "s16",
        "--raw",
    ]

    try:
      play_process = await asyncio.create_subprocess_exec(
          *cmd, stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
      )

      print("✓ Audio playback started with pw-cat")

      # Play buffered chunks first
      for chunk in initial_chunks:
        play_process.stdin.write(chunk)
        await play_process.stdin.drain()

      # Continue normal playback
      while True:
        bytestream = await self.audio_in_queue.get()
        play_process.stdin.write(bytestream)
        await play_process.stdin.drain()

    except Exception as e:
      print(f"❌ pw-cat playback error: {e}")



  async def _push_analysis_error(self, error_message: str):
    """Push analysis error to live model"""
    try:
      error_text = f"🚫 POSITION ANALYSIS ERROR: {error_message}"
      content = {"role": "user", "parts": [{"text": error_text}]}
      await self.session.send_client_content(turns=content, turn_complete=True)
      print(f"📤 Error message sent to user: {error_message}")
    except Exception as e:
      print(f"❌ Failed to push error to user: {e}")


  async def run(self):
    """Main simplified chess companion loop"""
    print("♟️  Starting Simplified Chess Companion...")
    print("👁️ Watching mode: Automatic analysis and commentary")
    print("🎧 Make sure to use headphones to prevent audio feedback!")
    print("💡 Type 'q' to quit")

    try:
      async with (
          self.client.aio.live.connect(
              model=MODEL, config=CHESS_CONFIG
          ) as session,
          asyncio.TaskGroup() as tg,
      ):
        self.session = session
        self.audio_in_queue = asyncio.Queue()

        # Start all tasks
        send_text_task = tg.create_task(self.send_text())
        
        # New background architecture: scene detection + fast FEN detection
        tg.create_task(self.detect_initial_board_mask())  # Seed the bounding box
        tg.create_task(self.start_scene_detection())       # Background scene detection  
        tg.create_task(self.fast_fen_detection_loop())     # Fast FEN checking
        
        tg.create_task(self.listen_user_audio())
        tg.create_task(self.transcribe_tv_audio())
        tg.create_task(self.receive_audio())
        tg.create_task(self.play_audio())

        await send_text_task

    except Exception as e:
      print(f"❌ Error: {e}")
      traceback.print_exc()
    finally:
      # Cleanup
      if hasattr(self, "engine_pool"):
        self.engine_pool.cleanup()
      if self.shared_cap:
        self.shared_cap.release()
      if self.mic_stream:
        self.mic_stream.close()
      if self.pya:
        self.pya.terminate()


if __name__ == "__main__":
  args = parse_args()
  companion = ChessCompanionSimplified(
      debug_mode=args.debug, watching_mode=not args.no_watch
  )
  asyncio.run(companion.run())
