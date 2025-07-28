# TV Companion Development Notes

## Project Overview
Building an LLM-powered TV companion that can watch and comment on movies/shows, similar to the Wind Waker companion but for video content.

## Audio Capture Breakthrough

### The Problem
Initial attempts to capture Chrome audio ran into several issues:
- Using speaker monitor captured ALL system audio (feedback loop with Gemini)
- `pipewire_python` library was outdated (`--list-targets` option doesn't exist)
- FIFO approach failed because `pw-cat` expects audio files, not raw streams

### The Solution: stdout Streaming
**SUCCESS**: Direct stdout streaming with raw PCM data works perfectly!

```bash
pw-cat --record - --target "Google Chrome" --rate 48000 --channels 2 --format f32 --raw
```

Key insights:
- Use `-` for stdout instead of filename
- Add `--raw` flag for raw PCM (no WAV headers)
- Target Chrome by node name: `--target "Google Chrome"`
- Read from `subprocess.PIPE` for streaming data
- Consistent 4800-byte chunks every 0.1 seconds

## Video Capture Challenges

### Chrome Window Capture Issues
Chrome video capture has several frustrating limitations:

**Root causes:**
- **Hardware acceleration**: Chrome uses GPU rendering for video, and when not active, the GPU may not render to the window buffer that screen capture can access
- **Browser optimization**: Chrome pauses/reduces rendering for background tabs to save resources
- **DRM/Copy protection**: Some video content actively prevents background capture
- **Window compositor**: System may not update non-visible window contents

**Solutions for Chrome:**
```python
# Option 1: Force foreground (simplest)
import pyautogui
pyautogui.getWindowsWithTitle("Chrome")[0].activate()
```

```bash
# Option 2: Chrome flags (disable optimizations)
google-chrome \
  --disable-gpu-sandbox \
  --disable-web-security \
  --disable-features=VizDisplayCompositor \
  --force-color-profile=srgb
```

```bash
# Option 3: Virtual display
Xvfb :99 -screen 0 1920x1080x24 &
DISPLAY=:99 google-chrome
```

### HDMI Capture Advantages
HDMI capture hardware solves these issues completely:

✅ **Signal-level capture**: Captures the actual video signal, not window buffers
✅ **Always active**: The display is receiving the signal regardless of focus
✅ **No browser optimizations**: Bypasses all Chrome-specific rendering issues
✅ **Hardware-level**: No software copy protection at window level
✅ **Consistent quality**: What you see is what you capture

**Development Strategy:**
- **Chrome development**: Keep window in foreground, use YouTube for testing
- **HDMI demos**: Professional quality, works with any content
- **Fallback plan**: Always have both options ready

## Content Source Strategies

### Chrome/Local Capture
**Pros:**
- Perfect for development and testing
- No HDCP copy protection issues
- Works anywhere with laptop
- Full browser control via Playwright/DevTools
- Reliable and consistent environment

**Cons:**
- Limited to browser content
- Less impressive for demos
- Requires local setup

**Actuation Methods:**
- Playwright/Selenium for browser automation
- Keyboard shortcuts (Space for pause, arrows for seek)
- Chrome DevTools Protocol for low-level control
- Direct DOM manipulation

### HDMI Capture (Hardware)
**Pros:**
- Works with any HDMI source (Netflix, cable TV, streaming boxes)
- More impressive demo factor
- Perfect for meeting rooms with existing displays
- Can capture "real" TV content

**Cons:**
- HDCP compliance unknown until tested
- Requires capture hardware setup
- More complex for development
- Limited actuation options

**Actuation Methods:**
- ADB commands for Android TV/Chromecast: `adb shell input keyevent KEYCODE_MEDIA_PAUSE`
- IR blaster for universal remote control
- HDMI CEC commands (if supported)
- Cast SDK APIs (limited to compatible apps)

## Demo Strategy

### Development Environment
Use Chrome capture for consistent, reliable development:
- YouTube videos for testing
- Controllable playback
- No HDCP issues
- Easy to set up anywhere

### Meeting Room Demos
HDMI capture from room displays:
- More impressive "wow factor"
- Real streaming service content (if HDCP allows)
- Fallback to Chrome if HDCP blocks content
- YouTube presentations always work

### Gemini Meetups/Workshops
Hybrid approach:
- Primary: HDMI capture from meeting room setup
- Fallback: Chrome capture on laptop
- Demo content: YouTube, local videos, or meeting presentations

## Technical Architecture

### Unified Controller Interface
```python
class TVController:
    def __init__(self, source_type="chrome"):
        self.source = source_type

    def pause(self):
        if self.source == "chrome":
            self.playwright_page.keyboard.press("Space")
        elif self.source == "hdmi":
            subprocess.run(["adb", "shell", "input", "keyevent", "KEYCODE_MEDIA_PAUSE"])

    def get_audio_stream(self):
        if self.source == "chrome":
            return self.chrome_audio_stream()
        elif self.source == "hdmi":
            return self.hdmi_audio_stream()
```

### Audio Pipeline
1. **Chrome**: `pw-cat` stdout streaming (working!)
2. **HDMI**: Capture device audio input (TODO)
3. **Processing**: Raw PCM → Gemini Live API
4. **Output**: Route Gemini to headphones (avoid feedback)

### Video Pipeline
1. **Chrome**: Screen capture via `mss` or browser APIs
2. **HDMI**: Hardware capture device
3. **Processing**: Screenshots → base64 → Gemini Live API

## Next Steps

### Immediate (Chrome Version)
- [ ] Integrate stdout audio streaming with TV companion
- [ ] Add screen capture from Chrome tab
- [ ] Implement basic Gemini Live API integration
- [ ] Test with YouTube content

### Hardware Integration
- [ ] Test HDMI capture device
- [ ] Verify HDCP compliance with various sources
- [ ] Implement hardware audio/video capture
- [ ] Add unified source switching

### Actuation Layer
- [ ] Chrome automation with Playwright
- [ ] ADB command integration for Android TV
- [ ] Test CEC commands if available
- [ ] IR blaster research for universal control

## Lessons Learned

1. **Library Dependencies**: Don't trust outdated wrapper libraries - test direct tools first
2. **Audio Formats**: FIFOs don't work with format-detecting tools like `pw-cat`
3. **stdout Streaming**: Simplest approach often works best
4. **Node Targeting**: Using node names ("Google Chrome") is simpler than parsing IDs
5. **Development vs Demo**: Need both Chrome (dev) and HDMI (demo) approaches

## HDCP Considerations

**Likely to work:**
- YouTube content
- Meeting room presentations
- Local video files
- Cable TV (varies by provider)

**Likely blocked:**
- Netflix, Disney+, Hulu (streaming services)
- Blu-ray players
- Some cable/satellite boxes

**Mitigation:**
- Always have Chrome fallback ready
- Test demo content beforehand
- Use YouTube for reliable demos
