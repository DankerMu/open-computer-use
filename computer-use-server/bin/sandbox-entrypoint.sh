#!/bin/bash
# Configure GitLab
if [ -n "$GITLAB_TOKEN" ]; then
    GITLAB_HOST="${GITLAB_HOST:-gitlab.com}"
    git config --global url."https://oauth2:${GITLAB_TOKEN}@${GITLAB_HOST}/".insteadOf "https://${GITLAB_HOST}/"
    echo "Git configured for $GITLAB_HOST with token auth"
else
    echo "No GITLAB_TOKEN - git/glab will work without auth (public repos only)"
fi

# Configure Claude Code
if [ -n "$ANTHROPIC_AUTH_TOKEN" ]; then
    export ANTHROPIC_AUTH_TOKEN
    export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-https://api.anthropic.com}"
    if [ -n "$ANTHROPIC_CUSTOM_HEADERS" ]; then
        export ANTHROPIC_CUSTOM_HEADERS
    fi
    echo "Claude Code configured with base URL: $ANTHROPIC_BASE_URL"
else
    echo "No ANTHROPIC_AUTH_TOKEN - Claude Code will not work"
fi

# Discoverability: how to escape sub-agent autostart
echo "Tip: plain bash with NO_AUTOSTART=1 bash  OR  touch /tmp/.no_autostart"

# Configure Playwright CLI for browser automation
cat > /home/assistant/playwright-cli.json << PCLIEOF
{
  "outputDir": "/mnt/user-data/files",
  "browser": {
    "browserName": "chromium",
    "launchOptions": {
      "channel": "chromium",
      "args": [
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--no-first-run",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-dev-shm-usage",
        "--disable-default-apps",
        "--disable-sync",
        "--disable-breakpad",
        "--disable-hang-monitor",
        "--disable-prompt-on-repost",
        "--metrics-recording-only",
        "--no-default-browser-check",
        "--window-size=1920,1080"
      ]
    },
    "contextOptions": {
      "navigationTimeout": 300000,
      "extraHTTPHeaders": {
      }
    }
  }
}
PCLIEOF

mkdir -p /home/assistant/.claude
# Write CLAUDE.md (environment info for Claude Code — same for interactive and MCP sub-agent)
cat > /home/assistant/.claude/CLAUDE.md << CLAUDEMDEOF
# Environment

## File Locations
- **Workspace**: /home/assistant (private working directory)
- **Workspace files**: /mnt/user-data/files (uploaded and generated files, read-write — auto-synced to preview)
- **Skills**: /mnt/skills/ (read-only, run: cat /mnt/skills/<name>/SKILL.md)

## Output Rules
- Save ALL user-facing files to /mnt/user-data/files/
- Files in /mnt/user-data/files automatically appear in the preview UI under the Files tab
- When telling the user where to find files, say: open the Files tab in the artifacts panel
- Workspace /home/assistant is for intermediate files only, not synced

## Plan Mode — MANDATORY
CRITICAL: You MUST enter plan mode BEFORE writing ANY code or creating files.
This applies to ALL tasks except single-line fixes (typos, variable renames).
If you skip plan mode, the user will reject your work.
If something goes sideways, STOP and re-plan immediately — do not keep pushing.
Plan must include: what files to create, architecture decisions, verification steps.

## Verification
Before completing any task:
1. Verify output files exist in /mnt/user-data/files/
2. Run the code or check the result
3. If tests exist, run them

## Self-Improvement
When you make a mistake, update this CLAUDE.md so you do not repeat it.

## Useful Commands
- ls /mnt/skills/ — list available skills
- cat /mnt/skills/<name>/SKILL.md — skill instructions
- ls /mnt/user-data/files/ — uploaded and generated files
- GSD (Get Shit Done): /gsd:help in Claude Code for spec-driven workflow commands
- Superpowers skills: test-driven-development, brainstorming, systematic-debugging, etc.

CLAUDEMDEOF
cat > /home/assistant/.claude/settings.json << CCEOF
{
  "permissions": {
    "allow": [
      "Bash(playwright-cli:*)",
      "Bash(*mnt/user-data/files*)",
      "Write(/mnt/user-data/files/**)",
      "Edit(/mnt/user-data/files/**)",
      "Read(/mnt/user-data/files/**)",
      "Read(/mnt/skills/**)",
      "Read(/home/assistant/.claude/**)",
      "Write(/home/assistant/.claude/CLAUDE.md)",
      "Write(/home/assistant/.claude/settings.json)",
      "Edit(/home/assistant/.claude/CLAUDE.md)",
      "Edit(/home/assistant/.claude/settings.json)",
      "Bash(gsd:*)",
      "Bash(ls*)",
      "Bash(cat*)",
      "Bash(mkdir*)",
      "Bash(cp*)",
      "Bash(mv*)"
    ]
  },
  "hooks": {
    "SessionStart": [
      {"hooks": [{"type": "command", "command": "[ -f /home/assistant/.claude/hooks/gsd-check-update.js ] && node /home/assistant/.claude/hooks/gsd-check-update.js || true"}]},
      {"hooks": [{"type": "command", "command": "[ -f /home/assistant/.claude/hooks/gsd-session-state.sh ] && bash /home/assistant/.claude/hooks/gsd-session-state.sh || true"}]}
    ],
    "PreToolUse": [
      {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "[ -f /home/assistant/.claude/hooks/gsd-prompt-guard.js ] && node /home/assistant/.claude/hooks/gsd-prompt-guard.js || true", "timeout": 5}]},
      {"matcher": "Read", "hooks": [{"type": "command", "command": "[ -f /home/assistant/.claude/hooks/gsd-read-guard.js ] && node /home/assistant/.claude/hooks/gsd-read-guard.js || true", "timeout": 5}]},
      {"matcher": "Bash", "hooks": [{"type": "command", "command": "[ -f /home/assistant/.claude/hooks/gsd-validate-commit.sh ] && bash /home/assistant/.claude/hooks/gsd-validate-commit.sh || true", "timeout": 5}]},
      {"matcher": "", "hooks": [{"type": "command", "command": "[ -f /home/assistant/.claude/hooks/gsd-workflow-guard.js ] && node /home/assistant/.claude/hooks/gsd-workflow-guard.js || true", "timeout": 5}]}
    ],
    "PostToolUse": [
      {"matcher": "Bash|Edit|Write|MultiEdit|Agent|Task", "hooks": [{"type": "command", "command": "[ -f /home/assistant/.claude/hooks/gsd-context-monitor.js ] && node /home/assistant/.claude/hooks/gsd-context-monitor.js || true", "timeout": 10}]},
      {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "[ -f /home/assistant/.claude/hooks/gsd-phase-boundary.sh ] && bash /home/assistant/.claude/hooks/gsd-phase-boundary.sh || true", "timeout": 5}]}
    ]
  }
}
CCEOF

# Skip Claude Code onboarding (theme picker, trust dialog)
# Write for both assistant (production) and root (test/fallback)
CLAUDE_JSON='{"hasCompletedOnboarding":true,"lastOnboardingVersion":"99.0.0","projects":{"/home/assistant":{"hasTrustDialogAccepted":true},"/":{"hasTrustDialogAccepted":true}}}'
echo "$CLAUDE_JSON" > /home/assistant/.claude.json
echo "$CLAUDE_JSON" > /root/.claude.json 2>/dev/null

# Symlink each skill individually so Claude Code /skills sees them (flat structure required)
mkdir -p /home/assistant/.claude/skills
mkdir -p /root/.claude/skills 2>/dev/null
for skilldir in /mnt/skills/public/ /mnt/skills/private/ /mnt/skills/user/; do
    for skill in ${skilldir}*/; do
        [ -d "$skill" ] && ln -sf "$skill" /home/assistant/.claude/skills/$(basename "$skill") 2>/dev/null
        [ -d "$skill" ] && ln -sf "$skill" /root/.claude/skills/$(basename "$skill") 2>/dev/null
    done
done

# External skills (GSD + Superpowers) — SYMLINK into volume to keep /home/assistant small
# (per-container volume — see commit 934197d "Move npm packages out of volume mount")
# GSD get-shit-done/ is read-only: cache lives in ~/.cache/gsd/, state in project .planning/
mkdir -p /home/assistant/.claude/agents /home/assistant/.claude/commands /home/assistant/.claude/hooks
if [ -d /opt/skills-external/gsd ]; then
    ln -sfn /opt/skills-external/gsd/get-shit-done /home/assistant/.claude/get-shit-done
    for f in /opt/skills-external/gsd/agents/*.md; do
        [ -e "$f" ] && ln -sfn "$f" /home/assistant/.claude/agents/$(basename "$f")
    done
    ln -sfn /opt/skills-external/gsd/commands/gsd /home/assistant/.claude/commands/gsd
    for h in /opt/skills-external/gsd/hooks/*; do
        [ -e "$h" ] && ln -sfn "$h" /home/assistant/.claude/hooks/$(basename "$h")
    done
fi
if [ -d /opt/skills-external/superpowers ]; then
    for d in /opt/skills-external/superpowers/skills/*/; do
        name=$(basename "$d")
        [ -e "/home/assistant/.claude/skills/$name" ] || ln -sfn "$d" "/home/assistant/.claude/skills/$name"
    done
    for f in /opt/skills-external/superpowers/commands/*.md; do
        [ -e "$f" ] && [ ! -e "/home/assistant/.claude/commands/$(basename "$f")" ] && ln -sfn "$f" /home/assistant/.claude/commands/$(basename "$f")
    done
    for f in /opt/skills-external/superpowers/agents/*.md; do
        [ -e "$f" ] && [ ! -e "/home/assistant/.claude/agents/$(basename "$f")" ] && ln -sfn "$f" /home/assistant/.claude/agents/$(basename "$f")
    done
fi
chown -R --no-dereference $(id -u assistant 2>/dev/null || echo 1000):$(id -g assistant 2>/dev/null || echo 1000) /home/assistant/.claude 2>/dev/null || true

# Copy CLAUDE.md and settings.json for root too
cp /home/assistant/.claude/CLAUDE.md /root/.claude/CLAUDE.md 2>/dev/null
cp /home/assistant/.claude/settings.json /root/.claude/settings.json 2>/dev/null

# Phase 6 — render per-CLI config files once per container (marker-gated).
# Marker is in /tmp (NOT volume) so an env-var change + restart re-renders
# from scratch (AUTH-04). Distinct from openwebui/init.sh persistent marker.
if [ ! -f /tmp/.cli-runtime-initialised ]; then
    case "${SUBAGENT_CLI:-claude}" in
        opencode)
            mkdir -p /tmp
            if [ -n "${OPENCODE_CONFIG_EXTRA:-}" ]; then
                printf "%s" "$OPENCODE_CONFIG_EXTRA" > /tmp/opencode.json
                echo "OpenCode config sourced from OPENCODE_CONFIG_EXTRA (operator override; canonical file skipped)"
            elif [ -f /opt/cli-defaults/opencode.json ]; then
                # D-09/D-10: strip _spdx/_copyright underscore-prefixed keys before passing to opencode CLI.
                python3 -c "import json,sys; d=json.load(open('/opt/cli-defaults/opencode.json')); [d.pop(k,None) for k in list(d) if k.startswith('_')]; json.dump(d,sys.stdout)" > /tmp/opencode.json
                echo "OpenCode config sourced from /opt/cli-defaults/opencode.json (canonical default)"
            else
                echo "FATAL: /opt/cli-defaults/opencode.json missing and OPENCODE_CONFIG_EXTRA unset" >&2
                exit 1
            fi
            export OPENCODE_CONFIG=/tmp/opencode.json
            ;;
        codex)
            mkdir -p /home/assistant/.codex
            if [ -n "${OPENAI_BASE_URL:-}" ]; then
                cat > /home/assistant/.codex/config.toml <<CXEOF
model_provider = "custom"

[model_providers.custom]
name = "custom-gateway"
base_url = "${OPENAI_BASE_URL}"
env_key = "OPENAI_API_KEY"
wire_api = "responses"
requires_openai_auth = true
CXEOF
                echo "Codex config rendered with custom gateway: $OPENAI_BASE_URL"
            elif [ -f /opt/cli-defaults/codex.json ]; then
                # D-09: read canonical JSON, convert to TOML. Empty model_providers baseline = public OpenAI defaults.
                python3 -c "
import json,sys
def _to_toml_value(v):
    if v is None:
        raise ValueError('TOML does not support null values; provider config must omit empty fields')
    if isinstance(v,dict):
        inner=', '.join(k+' = '+_to_toml_value(val) for k,val in v.items())
        return '{'+inner+'}'
    return json.dumps(v)
d=json.load(open('/opt/cli-defaults/codex.json'))
[d.pop(k,None) for k in list(d) if k.startswith('_')]
providers=d.get('model_providers',{}) or {}
default_model=d.get('default_model')
lines=[]
if default_model:
    lines.append('model = \"' + default_model + '\"')
if providers:
    first=next(iter(providers))
    lines.append('model_provider = \"' + first + '\"')
    lines.append('')
    for name,cfg in providers.items():
        lines.append('[model_providers.' + name + ']')
        for ck,cv in cfg.items():
            lines.append(ck + ' = ' + _to_toml_value(cv))
        lines.append('')
print('\n'.join(lines))
" > /home/assistant/.codex/config.toml
                echo "Codex config sourced from /opt/cli-defaults/codex.json (canonical default; converted to TOML)"
            else
                : > /home/assistant/.codex/config.toml
                echo "Codex config empty — public OpenAI defaults (no canonical file found)"
            fi
            if [ -n "${CODEX_CONFIG_EXTRA:-}" ]; then
                printf "\n# === CODEX_CONFIG_EXTRA (operator-supplied) ===\n%s\n" "$CODEX_CONFIG_EXTRA" >> /home/assistant/.codex/config.toml
                echo "Codex config extended via CODEX_CONFIG_EXTRA"
            fi
            chown -R assistant:assistant /home/assistant/.codex
            ;;
    esac
    touch /tmp/.cli-runtime-initialised
fi

# Auto-start chosen sub-agent CLI on first interactive bash login (both users).
# Honours SUBAGENT_CLI (default claude). Escape hatches: NO_AUTOSTART=1 env
# OR `touch /tmp/.no_autostart` from a second terminal to opt subsequent sessions out.
# Marker renamed from the old per-CLI name to SUBAGENT_AUTOSTARTED (independent
# check; existing volumes with the old marker still autostart exactly once on next session).
AUTOSTART_LINE='[ -z "$SUBAGENT_AUTOSTARTED" ] && [ -z "$NO_AUTOSTART" ] && [ ! -f /tmp/.no_autostart ] && [ -n "$PS1" ] && export SUBAGENT_AUTOSTARTED=1 && exec "${SUBAGENT_CLI:-claude}"'
for rc in /home/assistant/.bashrc /root/.bashrc; do
    if [ ! -f "$rc" ] || ! grep -q SUBAGENT_AUTOSTARTED "$rc" 2>/dev/null; then
        echo "$AUTOSTART_LINE" >> "$rc"
    fi
done

# Mark volume as active (used by cleanup script to calculate TTL from last use, not creation)
touch /home/assistant/.last_active

# Skill usage tracking: inotify watcher logs SKILL.md reads to the files bind mount
if command -v inotifywait >/dev/null 2>&1 && [ -d /mnt/skills ]; then
  (
    inotifywait -q -e access -m -r /mnt/skills/ --format "%w%f" 2>/dev/null |
    while IFS= read -r filepath; do
      if [[ "$filepath" == */SKILL.md ]]; then
        skill=$(basename "$(dirname "$filepath")")
        ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
        if [ -w /mnt/user-data/files ]; then
          echo "{\"ts\":\"$ts\",\"skill\":\"$skill\",\"email\":\"${GIT_AUTHOR_EMAIL:-unknown}\",\"chat_id\":\"${CHAT_ID:-unknown}\"}" >> /mnt/user-data/files/.skill-usage.jsonl 2>/dev/null || true
        fi
      fi
    done
  ) &
fi

exec "$@"
