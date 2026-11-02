import asyncio,json,os
from pathlib import Path
from playwright.async_api import async_playwright

root=Path(__file__).resolve().parent
async def main():
    checks=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(executable_path=os.environ.get('CLIPS_DESIGN_BROWSER','/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),headless=True)
        page=await browser.new_page(viewport={'width':1440,'height':1100},device_scale_factor=1)
        errors=[]
        page.on('pageerror',lambda error: errors.append(str(error)))
        await page.goto((root/'proposal.html').as_uri())
        await page.get_by_role('checkbox').nth(0).check()
        assert await page.evaluate('selected.size') == 1
        assert await page.evaluate("document.activeElement.id") == 'pick-a'
        await page.get_by_role('checkbox').nth(1).check()
        assert await page.evaluate('selected.size') == 2
        await page.get_by_role('button',name='查看生成说明',exact=True).click()
        assert await page.get_by_role('dialog').is_visible()
        assert '2 段视频' in await page.locator('#dialog-title').inner_text()
        await page.get_by_role('button',name='暂不开始',exact=True).click()
        checks.append({'check':'selection_and_confirmation','passed':True})
        for width in [390,320,768,1024,1440]:
            await page.set_viewport_size({'width':width,'height':900})
            await page.evaluate("setView('candidates')")
            size=await page.evaluate('({viewport:innerWidth,document:document.documentElement.scrollWidth})')
            assert size['viewport']==width and size['document']<=width,size
            checks.append({'check':'candidates_width','width':width,'passed':True})
            if width==390:
                await page.get_by_role('button',name='查看原文',exact=True).nth(0).click()
                assert await page.get_by_role('dialog').is_visible()
                await page.keyboard.press('Escape')
                assert not await page.get_by_role('dialog').is_visible()
                checks.append({'check':'mobile_original_text_dialog','passed':True})
        await page.set_viewport_size({'width':320,'height':900})
        await page.evaluate("setView('settings')")
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        checks.append({'check':'settings_320','passed':True})
        await page.evaluate("setView('candidates')")
        await page.locator('#scenario').select_option('missing')
        assert await page.get_by_role('button',name='查看生成说明',exact=True).is_enabled()
        checks.append({'check':'missing_source_does_not_block_cache_quote','passed':True})
        await page.locator('#scenario').select_option('audio')
        assert await page.get_by_role('checkbox').count()==0
        assert await page.get_by_role('button',name='当前内容无法导出视频',exact=True).is_disabled()
        checks.append({'check':'audio_analysis_without_video_export','passed':True})
        await page.locator('#scenario').select_option('video')
        await page.set_viewport_size({'width':1440,'height':1050})
        await page.evaluate("setView('results')")
        assert await page.get_by_role('button',name='下载视频',exact=True).count()==2
        checks.append({'check':'separate_result_files_and_expiry','passed':True})
        for state in ['generating','empty']:
            await page.evaluate(f"setView('{state}')")
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            checks.append({'check':state,'passed':True})
        assert not errors,errors
        checks.append({'check':'javascript_errors','passed':True,'errors':errors})
        # Capture each layout in a fresh page, avoiding compositor artifacts after many resizes.
        for name,width,height,query in [('desktop-candidates',1440,1100,'selected=2'),('mobile-candidates',390,900,'selected=2'),('desktop-results',1440,1050,'view=results'),('narrow-settings',320,900,'view=settings')]:
            shot=await browser.new_page(viewport={'width':width,'height':height},device_scale_factor=1)
            await shot.goto((root/'proposal.html').as_uri()+'?'+query)
            await shot.evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
            await shot.wait_for_timeout(100)
            assert await shot.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await shot.screenshot(path=str(root/(name+'.png')),full_page=True)
            if name=='mobile-candidates':
                assert not await shot.locator('.selection').evaluate("node=>node.classList.contains('is-sticky')")
                await shot.evaluate("scrollTo(0,document.querySelector('.heading').getBoundingClientRect().top+scrollY-16)")
                await shot.wait_for_timeout(120)
                assert await shot.locator('.selection').evaluate("node=>node.classList.contains('is-sticky')")
                checkbox=await shot.get_by_role('checkbox').nth(0).bounding_box()
                footer=await shot.locator('.selection').bounding_box()
                assert checkbox['y']+checkbox['height'] < footer['y']
                await shot.screenshot(path=str(root/'mobile-review.png'))
                checks.append({'check':'mobile_footer_activates_after_content_without_covering_first_selection','passed':True})
            await shot.close()
        for state in ['settings','candidates','generating','results','empty']:
            await page.set_viewport_size({'width':320,'height':900})
            await page.evaluate(f"setView('{state}')")
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            checks.append({'check':'all_states_320','state':state,'passed':True})
        await page.locator('#scenario').select_option('audio')
        await page.evaluate("setView('results')")
        assert await page.get_by_role('button',name='下载视频',exact=True).count()==0
        checks.append({'check':'audio_cannot_show_video_results','passed':True})
        await browser.close()
    (root/'render-checks.json').write_text(json.dumps({'kind':'design_prototype_only','business_api_calls':0,'checks':checks},ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'passed':len(checks),'business_api_calls':0,'checks':checks},ensure_ascii=False))
asyncio.run(main())
