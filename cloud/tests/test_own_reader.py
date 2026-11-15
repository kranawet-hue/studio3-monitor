"""Browser integration tests for the Android account reader, with fictional data.

No credentials or production site requests. The HTML models the observed
Studio3 popup structure; these tests do not replace an Android WebView test.
"""
import asyncio
import json
import os
from pathlib import Path
import pytest
from playwright.async_api import async_playwright
from tracker import BASE

SCRIPT = Path(__file__).resolve().parents[2] / 'android/app/src/main/assets/read-own.js'

async def run_reader(case):
    days=''.join(f'<th id="h{i}">Mo {16+i}.11.</th>' for i in range(7))
    cells=''.join(f'<div id="d{i}"></div>' for i in range(1,7))
    chips=''.join(f'''<div class="chip" title="Testkurs {n}" onclick="vs(event,{n})">
        <div class="head">18:00 − 19:00</div><div class="chip-title">Testkurs {n}<small>(4/10)</small></div></div>''' for n in (1,2,3))
    html=f'''<html><head><meta charset="utf-8"></head><body>Als test@example.invalid angemeldet
    <table id="container" class="weekCont"><tr>{days}</tr></table><div id="d0">{chips}</div>{cells}
    <input id="bbox_slot_id"><div id="bbox_content"></div>
    <script>
    const scenario={json.dumps(case)};
    function vs(event,id) {{
      document.getElementById('bbox_slot_id').value=String(id);
      const box=document.getElementById('bbox_content');
      if(id===1) {{box.textContent='Sie haben keine Reservierungen für diese Stunde';return;}}
      const email=scenario==='foreign'?'someone@example.invalid':'test@example.invalid';
      let rows='';
      for(let i=0;i<id-1;i++) rows+='<tr id="c'+(id*10+i)+'"><td>'+email+'</td><td>15.11.2026</td><td>'+(scenario==='waitlist'&&i===1?'<span class="wl">W</span>':'')+'</td></tr>';
      box.innerHTML='<table><tr><th>'+(scenario==='schema'?'Unbekannt':'Erstellt von')+'</th><th>Erstellt am</th><td></td></tr>'+rows+'</table>';
    }}
    </script></body></html>'''
    async with async_playwright() as p:
        browser=await p.chromium.launch(executable_path=os.getenv('CHROMIUM_EXECUTABLE') or None)
        page=await browser.new_page()
        await page.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body=html))
        await page.goto(BASE)
        script=SCRIPT.read_text(encoding='utf8').replace('__WEEK_JSON__','"2026-11-16"')
        await page.evaluate(script)
        await page.wait_for_function("['done','error'].includes(window.__studio3Read.state)",timeout=15000)
        result=await page.evaluate('window.__studio3Read')
        await browser.close()
        return result

def test_counts_zero_one_two_without_private_identifiers():
    result=asyncio.run(run_reader('normal'))
    assert result['state']=='done', result
    assert [c['own_confirmed'] for c in result['courses']]==[0,1,2]
    assert '@' not in json.dumps(result)
    assert all('reservation_id' not in c for c in result['courses'])

def test_waitlist_not_counted_as_confirmed_registration():
    result=asyncio.run(run_reader('waitlist'))
    assert result['state']=='done', result
    assert result['courses'][2]['own_confirmed']==1
    assert result['courses'][2]['own_waitlist']==1

@pytest.mark.parametrize('case',['foreign','schema'])
def test_ambiguous_ownership_or_layout_is_not_zero(case):
    result=asyncio.run(run_reader(case))
    assert result['state']=='error'
    assert 'courses' not in result
