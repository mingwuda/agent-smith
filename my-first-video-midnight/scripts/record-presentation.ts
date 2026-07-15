import { chromium } from "playwright";
import { execSync } from "child_process";
import * as fs from "fs";
import * as path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const ROOT = path.resolve(__dirname, "..");
const OUT_DIR = path.join(ROOT, "public", "video");
const URL = process.env.PRESENTATION_URL || "http://localhost:5174/?auto=1";
const CHAPTER_ID = "intro";

// ── helpers ────────────────────────────────────────────────────────
function getAudioDuration(file: string): number {
  try {
    const out = execSync(
      `ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "${file}"`,
      { encoding: "utf8" }
    ).trim();
    return Math.ceil(parseFloat(out) * 1000);
  } catch {
    return 5000; // fallback 5s
  }
}

function getChapterSteps(): { step: number; duration: number }[] {
  const dir = path.join(ROOT, "public", "audio", CHAPTER_ID);
  if (!fs.existsSync(dir)) {
    console.error(`✗ Audio dir not found: ${dir}`);
    console.error("  Run: npm run synthesize-audio");
    process.exit(1);
  }
  const files = fs
    .readdirSync(dir)
    .filter((f) => f.endsWith(".mp3"))
    .sort((a, b) => parseInt(a) - parseInt(b));

  return files.map((f) => {
    const step = parseInt(path.basename(f, ".mp3"));
    const dur = getAudioDuration(path.join(dir, f));
    return { step, duration: dur };
  });
}

// ── main ───────────────────────────────────────────────────────────
async function main() {
  const steps = getChapterSteps();
  const totalDuration = steps.reduce((s, x) => s + x.duration, 0);
  console.log(`Found ${steps.length} audio segments, total ~${(totalDuration / 1000).toFixed(1)}s`);

  fs.mkdirSync(OUT_DIR, { recursive: true });
  const outPath = path.join(OUT_DIR, "presentation.mp4");

  console.log("Launching browser...");
  const browser = await chromium.launch({
    headless: true,
    executablePath: "/usr/bin/ungoogled-chromium",
    args: [
      "--no-sandbox",
      "--disable-setuid-sandbox",
      "--disable-gpu",
      "--disable-dev-shm-usage",
    ],
  });

  const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 },
    recordVideo: { dir: OUT_DIR, size: { width: 1920, height: 1080 } },
  });

  const page = await context.newPage();
  
  // First, navigate to a blank page to clear any stale state
  await page.goto("about:blank");
  await page.evaluate(() => {
    try { localStorage.clear(); } catch {}
  });
  console.log("Cleared all localStorage");

  // Now navigate to the presentation
  console.log(`Navigating to ${URL}...`);
  await page.goto(URL, { waitUntil: "networkidle" });

  // Wait for the stage to be visible
  await page.waitForSelector(".stage-frame", { timeout: 10000 });
  await page.waitForTimeout(1000);

  // Start auto mode: press space to dismiss AutoStartGate
  console.log("Starting auto mode...");
  await page.keyboard.press("Space");
  await page.waitForTimeout(500);

  // Record for the total duration + buffer
  const recordMs = totalDuration + 3000;
  console.log(`Recording for ~${(recordMs / 1000).toFixed(1)}s...`);

  // Wait for recording to complete
  await page.waitForTimeout(recordMs);

  // Stop recording by closing context
  await context.close();
  await browser.close();

  // Playwright saves as .webm, rename to .mp4
  const videoFiles = fs
    .readdirSync(OUT_DIR)
    .filter((f) => f.endsWith(".webm") || f.endsWith(".mp4"));

  if (videoFiles.length === 0) {
    console.error("✗ No video file found in output dir");
    process.exit(1);
  }

  const src = path.join(OUT_DIR, videoFiles[0]);
  if (src !== outPath) {
    fs.renameSync(src, outPath);
  }

  console.log(`✓ Video saved to: ${outPath}`);
  console.log(`  Size: ${(fs.statSync(outPath).size / 1024 / 1024).toFixed(1)} MB`);
}

main().catch((e) => {
  console.error("✗ Recording failed:", e);
  process.exit(1);
});
