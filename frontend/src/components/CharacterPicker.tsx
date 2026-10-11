/**
 * CharacterPicker — compact header showing the currently selected
 * character (name + avatar thumbnail), expandable into a grid to switch.
 *
 * See docs/RAG_character_roleplay_implementation_plan.md Stage 7.
 */

import { useState } from 'react'
import { useChatStore } from '../store/chatStore'

function CharacterAvatarThumb({ src, alt, size = 28 }: { src: string | null; alt: string; size?: number }) {
  const [failed, setFailed] = useState(false)
  const initial = alt.charAt(0).toUpperCase() || '?'

  if (!src || failed) {
    return (
      <div
        style={{
          width: size,
          height: size,
          borderRadius: '50%',
          background: 'linear-gradient(135deg, #60a5fa, #a78bfa)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: '#0b0b12',
          fontWeight: 700,
          fontSize: size * 0.42,
          flexShrink: 0,
        }}
        aria-hidden="true"
      >
        {initial}
      </div>
    )
  }

  return (
    <img
      src={src}
      alt={alt}
      onError={() => setFailed(true)}
      style={{
        width: size,
        height: size,
        borderRadius: '50%',
        objectFit: 'cover',
        flexShrink: 0,
        background: '#20202c',
      }}
    />
  )
}

export function CharacterPicker() {
  const { characters, currentCharacterId, setCharacter } = useChatStore()
  const [open, setOpen] = useState(false)

  const current = characters.find((c) => c.id === currentCharacterId)

  if (characters.length === 0) return null

  return (
    <div style={s.wrap}>
      <button
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        onBlur={() => window.setTimeout(() => setOpen(false), 150)}
        style={s.trigger}
      >
        <CharacterAvatarThumb src={current?.avatar_thumbnail ?? current?.avatar ?? null} alt={current?.display_name ?? '?'} />
        <span style={s.triggerText}>{current?.display_name ?? 'Select character'}</span>
        <span aria-hidden="true" style={s.chevron}>⌄</span>
      </button>

      {open && (
        <div role="listbox" style={s.panel}>
          {characters.map((character) => {
            const selected = character.id === currentCharacterId
            return (
              <button
                key={character.id}
                type="button"
                role="option"
                aria-selected={selected}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => {
                  setCharacter(character.id)
                  setOpen(false)
                }}
                style={{ ...s.option, ...(selected ? s.optionSelected : null) }}
              >
                <CharacterAvatarThumb
                  src={character.avatar_thumbnail ?? character.avatar}
                  alt={character.display_name}
                  size={36}
                />
                <div style={s.optionText}>
                  <span style={s.optionName}>{character.display_name}</span>
                  {character.description && <span style={s.optionDesc}>{character.description}</span>}
                </div>
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

const s: Record<string, React.CSSProperties> = {
  wrap: {
    position: 'relative',
    display: 'inline-flex',
    fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace",
  },
  trigger: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    padding: '6px 12px 6px 6px',
    background: 'rgba(255,255,255,0.05)',
    border: '1px solid rgba(255,255,255,0.08)',
    borderRadius: 20,
    cursor: 'pointer',
    outline: 'none',
    fontFamily: 'inherit',
  },
  triggerText: {
    color: '#e2e8f0',
    fontSize: 13,
    fontWeight: 600,
  },
  chevron: {
    color: '#94a3b8',
    fontSize: 14,
    lineHeight: 1,
  },
  panel: {
    position: 'absolute',
    top: 'calc(100% + 6px)',
    left: 0,
    zIndex: 1000,
    width: 300,
    maxHeight: 320,
    overflowY: 'auto',
    padding: 6,
    display: 'flex',
    flexDirection: 'column',
    gap: 4,
    background: '#20202c',
    border: '1px solid rgba(255,255,255,0.12)',
    borderRadius: 10,
    boxShadow: '0 14px 32px rgba(0,0,0,0.4)',
  },
  option: {
    display: 'flex',
    alignItems: 'center',
    gap: 10,
    padding: '8px 10px',
    background: 'transparent',
    border: 0,
    borderRadius: 8,
    cursor: 'pointer',
    textAlign: 'left',
    fontFamily: 'inherit',
  },
  optionSelected: {
    background: 'rgba(96,165,250,0.16)',
  },
  optionText: {
    display: 'flex',
    flexDirection: 'column',
    gap: 2,
    minWidth: 0,
  },
  optionName: {
    color: '#f1f5f9',
    fontSize: 13,
    fontWeight: 700,
  },
  optionDesc: {
    color: '#94a3b8',
    fontSize: 11,
    lineHeight: 1.35,
    overflow: 'hidden',
    display: '-webkit-box',
    WebkitLineClamp: 2,
    WebkitBoxOrient: 'vertical',
  },
}
