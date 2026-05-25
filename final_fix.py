import re

dl_path = "dashboard_live.html"
with open(dl_path, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Replace the iframes
pattern = r'<div class="ap-section-title">Intraday Chart</div>\s*<div class="chart-tf-bar" id="chartTfBar">.*?<iframe id="tvChartFrame".*?</iframe>\s*</div>'

new_str = """<div class="ap-section-title" style="display:flex; justify-content:space-between; align-items:center;">
        <span>Intraday Price &amp; OI Chart</span>
      </div>
      
      <div class="dashboard-controls" style="display: flex; justify-content: space-between; align-items: center; padding: 4px 0 12px 0;">
        <div style="display: flex; gap: 8px;" id="chartTfBar">
          <button class="ts-select" data-interval="3minute" onclick="switchChartTf('${sym}','3minute',this)">3m</button>
          <button class="ts-select active" data-interval="5minute" onclick="switchChartTf('${sym}','5minute',this)">5m</button>
          <button class="ts-select" data-interval="15minute" onclick="switchChartTf('${sym}','15minute',this)">15m</button>
        </div>
        <div style="display: flex; align-items: center; gap: 6px;">
          <span class="stat-label" style="margin-bottom: 0; font-size: 9px; color: var(--text-dim); letter-spacing: 0.5px;">LOWER PANE:</span>
          <div style="display: flex; background: rgba(0,0,0,0.3); border: 1px solid var(--border); border-radius: 4px; padding: 1.5px;" id="chartBottomControls">
            <button class="ts-select" style="background: transparent; border: 1px solid transparent; color: var(--text-dim); border-radius: 3px; padding: 2.5px 8px; font-size: 9px; cursor: pointer; transition: all 0.2s; font-weight: 600;" onclick="toggleBottomChartMode('total', this)">Total OI</button>
            <button class="ts-select active" style="background: var(--blue-soft); border: 1px solid rgba(59,130,246,0.3); color: var(--text-bright); border-radius: 3px; padding: 2.5px 8px; font-size: 9px; cursor: pointer; transition: all 0.2s; font-weight: 600;" onclick="toggleBottomChartMode('options', this)">PE-CE Delta</button>
          </div>
        </div>
      </div>
      
      <div class="tv-chart-wrap" id="tvChartWrap" style="height: 380px; display: flex; flex-direction: column; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--bg-card); overflow: hidden; position: relative;">
        <div id="chartLoadingOverlay" style="position: absolute; top:0; left:0; right:0; bottom:0; background: var(--bg-card); display: flex; align-items: center; justify-content: center; z-index: 10; color: var(--text-dim); font-size: 13px; font-family: inherit;">
          <div style="display:flex; align-items:center; gap:8px;">
            <span class="pulse-dot active" style="width:8px; height:8px; background:var(--accent);"></span>
            Loading Intraday Price &amp; OI Chart...
          </div>
        </div>
        <div id="tvChart" style="flex: 2; min-height: 220px; border-bottom: 1px solid var(--border); position: relative;"></div>
        <div id="tvChartOi" style="flex: 1.2; min-height: 130px; position: relative;"></div>
        ${!(window.currentUser && (window.currentUser.plan === 'pro' || window.currentUser.role === 'admin')) ? `
        <div style="position: absolute; top:0; left:0; right:0; bottom:0; background: rgba(10, 10, 15, 0.6); backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px); display: flex; flex-direction: column; align-items: center; justify-content: center; z-index: 20; color: var(--text);">
          <div style="margin-bottom: 12px; font-size: 24px;">🔒</div>
          <div style="font-weight: 600; margin-bottom: 8px;">Premium Feature</div>
          <div style="font-size: 13px; color: var(--text-dim); margin-bottom: 16px; text-align: center; padding: 0 20px;">Advanced Interactive Charts are locked under the Pro subscription.</div>
          <a href="/settings#billing" class="btn" style="text-decoration:none; background:var(--gold); color:#000;">Upgrade to Pro</a>
        </div>
        ` : ''}
      </div>"""

content = re.sub(pattern, new_str, content, flags=re.DOTALL)

# 2. Add initLightweightChart call to openAnalysis
content = content.replace("document.getElementById('analysisOverlay').classList.add('open');", 
                          "document.getElementById('analysisOverlay').classList.add('open');\n  setTimeout(() => { initLightweightChart(sym, currentChartInterval); }, 50);")

content = content.replace("body.innerHTML = `", "setTimeout(() => { initLightweightChart(sym, currentChartInterval); }, 50);\n  body.innerHTML = `")

# 3. Append JS
with open("add_lightweight_chart_js.py", "r", encoding="utf-8") as f:
    add_code = f.read()

# Extract just the lwc_js string
lwc_start = add_code.find('lwc_js = """\n') + len('lwc_js = """\n')
lwc_end = add_code.rfind('"""\n\nif "function')
lwc_js = add_code[lwc_start:lwc_end]

content = re.sub(r'</body>', '<script>\n' + lwc_js + '\n</script>\n</body>', content)

with open(dl_path, "w", encoding="utf-8") as f:
    f.write(content)

print("Final fix applied successfully.")

