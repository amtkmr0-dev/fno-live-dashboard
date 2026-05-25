with open('nifty.html', 'r') as f:
    content = f.read()

# Make the toggle buttons very bright blue and prominent
old_btn = 'style="background: transparent; border: none; color: var(--text-dim); cursor: pointer; padding: 2px;"'
new_btn = 'style="background: rgba(59, 130, 246, 0.15); border: 1px solid rgba(59, 130, 246, 0.3); border-radius: 4px; color: var(--blue); cursor: pointer; padding: 4px; display: flex; align-items: center; justify-content: center; box-shadow: 0 0 8px rgba(59,130,246,0.2);"'

content = content.replace(old_btn, new_btn)

# Make the chevron slightly thicker
content = content.replace('stroke-width="2"', 'stroke-width="2.5"')

with open('nifty.html', 'w') as f:
    f.write(content)
