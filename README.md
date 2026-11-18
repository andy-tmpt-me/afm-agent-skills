# Agent skills from Answers From Me

Skills we use with our coding agents at [Answers From Me](https://answersfrom.me). Each one is a folder
with a `SKILL.md` and, where it needs them, scripts. They follow the open [Agent Skills](https://agentskills.io)
format, so the same folder works in Claude Code, Cursor, Gemini CLI and Codex.

## The skills

| Skill | What it does | Needs |
| --- | --- | --- |
| [`map-pr`](skills/map-pr/SKILL.md) | Keep a live briefing page for a repo's open pull requests, so a reviewer sees the context, the size and the risks of each one before reading code. | gh (signed in), git and python3 3.11 or later |

See one on a real repo: [map-pr on flutter/flutter](https://andy-tmpt-me.github.io/afm-agent-skills/map-pr/flutter/),
a snapshot of Flutter's open pull requests.

## Install a skill

Copy the skill's folder into your agent's skills folder. For Claude Code, in every project:

```
git clone https://github.com/andy-tmpt-me/afm-agent-skills.git
cp -R afm-agent-skills/skills/map-pr ~/.claude/skills/
```

| Agent | Skills folder in a project | For every project |
| --- | --- | --- |
| Claude Code | `<project>/.claude/skills` | `~/.claude/skills` |
| Cursor | `<project>/.cursor/skills` | `~/.cursor/skills` |
| Gemini CLI | `<project>/.gemini/skills` | `~/.gemini/skills` |
| Codex | `<project>/.agents/skills` | `~/.agents/skills` |

Then ask your agent for it by name, for example "map-pr 1234". Each skill's `SKILL.md` says what it takes and what
it needs installed.

## Changes

These skills are published from the repo where we work on them, and each publish replaces the folders here. A pull
request against this repo would be overwritten, so please open an issue instead and we'll make the change at the
source.

## License

[MIT](LICENSE)
