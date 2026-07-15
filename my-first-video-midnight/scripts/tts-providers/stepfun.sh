# ────────────────────────────────────────────────────────────────────
# StepFun provider — uses the official StepAudio TTS API.
#
# Docs:  https://platform.stepfun.com/docs/audio/audio-speech
# Model: step-tts-mini
#
# Strengths: Chinese narration quality is good; simple REST API;
# no extra CLI dependency needed.
# ────────────────────────────────────────────────────────────────────

tts_check() {
  if [[ -z "${STEP_API_KEY:-}" ]]; then
    echo "✗ STEP_API_KEY is not set." >&2
    return 1
  fi
}

tts_install_help() {
  cat <<'EOF' >&2
To use the StepFun provider:

  Export your API key:
    export STEP_API_KEY="your-stepfun-api-key"

  Then run:
    PRESENTATION_TTS=stepfun npm run synthesize-audio

  Get a key at https://platform.stepfun.com
EOF
}

tts_synthesize() {
  local text="$1"
  local out="$2"
  local voice="${3:-cixingnansheng}"

  curl --silent --location 'https://api.stepfun.com/v1/audio/speech' \
    --header 'Content-Type: application/json' \
    --header "Authorization: Bearer ${STEP_API_KEY}" \
    --data "$(jq -n \
      --arg model "step-tts-mini" \
      --arg input "$text" \
      --arg voice "$voice" \
      '{model: $model, input: $input, voice: $voice}')" \
    --output "$out" \
    >/dev/null 2>&1
}
