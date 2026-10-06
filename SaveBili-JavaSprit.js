// ==UserScript==
// @name         SaveBili - B站全场景下载助手
// @namespace    https://github.com/savebili
// @version      2.2.0
// @description  支持B站投稿视频、番剧、专栏、动态下载，下载按钮位于弹幕发送键右侧，下载文件自带声音
// @author       SaveBili
// @license      MIT
// @icon         https://www.bilibili.com/favicon.ico
// @tag          下载工具
// @tag          B站
// @tag          视频下载
// @tag          实用工具
// @category     下载工具
// @match        *://www.bilibili.com/video/*
// @match        *://www.bilibili.com/bangumi/play/*
// @match        *://www.bilibili.com/read/*
// @match        *://t.bilibili.com/*
// @match        *://www.bilibili.com/opus/*
// @match        *://space.bilibili.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_download
// @grant        GM_setValue
// @grant        GM_getValue
// @connect      *
// @run-at       document-idle
// ==/UserScript==

(function () {
    'use strict';

    const DEFAULT_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36';
    const DEBUG = true;
    const log = (...args) => { if (DEBUG) console.log('[SaveBili]', ...args); };

    // ==================== 场景检测 ====================
    function getPageInfo() {
        const url = location.href;
        let m;
        m = url.match(/\/video\/(BV[0-9A-Za-z]{10}|av\d+)/i);
        if (m) return { type: 'video', id: m[1] };
        m = url.match(/\/bangumi\/play\/(ep|ss)(\d+)/i);
        if (m) return { type: 'bangumi', id: m[1] + m[2] };
        m = url.match(/\/read\/(cv\d+)/i);
        if (m) return { type: 'article', id: m[1] };
        m = url.match(/\/opus\/(\d+)/);
        if (m) return { type: 'opus', id: m[1] };
        m = url.match(/t\.bilibili\.com\/(\d+)/);
        if (m) return { type: 'opus', id: m[1] };
        m = url.match(/space\.bilibili\.com\/(\d+)/);
        if (m) return { type: 'space', id: m[1] };
        return null;
    }

    // ==================== 请求封装 ====================
    function gmRequest(options) {
        return new Promise((resolve, reject) => {
            GM_xmlhttpRequest({
                method: options.method || 'GET',
                url: options.url,
                headers: Object.assign({
                    'User-Agent': DEFAULT_UA,
                    'Referer': 'https://www.bilibili.com/',
                }, options.headers || {}),
                responseType: options.responseType || 'json',
                timeout: 20000,
                onload: (res) => {
                    log('API 响应:', options.url.substring(0, 100), '状态:', res.status);
                    if (res.status < 200 || res.status >= 300) {
                        return reject(new Error(`HTTP ${res.status}`));
                    }
                    if (res.response === null || res.response === undefined) {
                        if (!res.responseText) {
                            return reject(new Error('服务器返回空数据（可能是登录态失效或内容被限制）'));
                        }
                    }
                    try {
                        const data = res.response || (res.responseText ? JSON.parse(res.responseText) : null);
                        if (data === null || data === undefined) {
                            return reject(new Error('服务器返回数据为空'));
                        }
                        resolve(data);
                    } catch (e) {
                        reject(new Error('响应解析失败: ' + e.message));
                    }
                },
                onerror: () => reject(new Error('网络请求失败（请检查 @connect 授权）')),
                ontimeout: () => reject(new Error('请求超时')),
            });
        });
    }
    const fetchJson = (url, headers) => gmRequest({ url, headers });

    // 判断是否为"清晰度不可用"错误（B站返回 no data / -404 / -400）
    function isQualityError(err) {
        const msg = (err && err.message) || '';
        return /no\s*data/i.test(msg) || /-?404/.test(msg) || /-?400/.test(msg);
    }

    // ==================== 视频信息解析 ====================
    async function getVideoInfo(bvid) {
        const api = bvid.toLowerCase().startsWith('av')
            ? `https://api.bilibili.com/x/web-interface/view?aid=${bvid.slice(2)}`
            : `https://api.bilibili.com/x/web-interface/view?bvid=${bvid}`;
        const data = await fetchJson(api);
        if (data.code !== 0) throw new Error(data.message || `视频信息获取失败 code=${data.code}`);
        const d = data.data;
        if (!d) throw new Error('视频信息为空（视频可能已下架或需要登录）');
        if (!d.pages || !d.pages.length) throw new Error('视频无可用分P');
        return {
            bvid: d.bvid, aid: d.aid, cid: d.cid,
            title: d.title || 'untitled',
            desc: d.desc || '', pic: d.pic || '',
            owner: (d.owner && d.owner.name) || '',
            pages: d.pages.map(p => ({
                cid: p.cid, page: p.page, part: p.part || ('P' + p.page), duration: p.duration,
            })),
        };
    }

    async function getBangumiInfo(epOrSs) {
        const url = epOrSs.startsWith('ep')
            ? `https://api.bilibili.com/pgc/view/web/season?ep_id=${epOrSs.slice(2)}`
            : `https://api.bilibili.com/pgc/view/web/season?season_id=${epOrSs.slice(2)}`;
        const data = await fetchJson(url);
        if (data.code !== 0) throw new Error(data.message || `番剧信息获取失败 code=${data.code}`);
        const d = data.data || data.result;
        if (!d) throw new Error('番剧数据为空（可能需要登录或为付费内容）');
        const eps = [...(d.episodes || [])];
        (d.section || []).forEach(s => eps.push(...(s.episodes || [])));
        if (!eps.length) throw new Error('番剧无可用剧集');
        return {
            title: d.title || '番剧',
            cover: d.cover || '',
            episodes: eps.map(ep => ({
                ep_id: ep.id, cid: ep.cid, aid: ep.aid || 0,
                title: ep.share_copy || ep.long_title || ep.title || '',
                index: ep.title || '',
            })),
        };
    }

    // ==================== 播放地址解析（核心修复：自动降级清晰度） ====================
    function qualityLabel(qn) {
        const map = { 120: '4K', 116: '1080P60', 112: '1080P+', 80: '1080P', 74: '720P60', 64: '720P', 32: '480P', 16: '360P' };
        return map[qn] || `qn${qn}`;
    }

    // 单次请求播放地址
    async function fetchPlayurlOnce(aid, cid, bvid, quality) {
        const url = `https://api.bilibili.com/x/player/playurl?avid=${aid}&cid=${cid}` +
                    `&qn=${quality}&otype=json&platform=pc&fnver=0&fnval=1&fourk=1`;
        const headers = bvid ? { Referer: `https://www.bilibili.com/video/${bvid}` } : {};
        const data = await fetchJson(url, headers);
        if (data.code !== 0) {
            const err = new Error(data.message || `播放地址获取失败 code=${data.code}`);
            err.code = data.code;
            throw err;
        }
        const d = data.data || data.result;
        if (!d) throw new Error('播放数据为空');
        return d;
    }

    // 带自动降级：目标清晰度失败时，自动尝试更低清晰度
    async function getPlayurl(aid, cid, bvid, quality) {
        const fallbacks = [quality, 80, 64, 32, 16]
            .filter((v, i, a) => a.indexOf(v) === i && v <= quality);
        let lastErr = null;
        for (const qn of fallbacks) {
            try {
                log(`尝试清晰度 qn=${qn} (${qualityLabel(qn)})`);
                const d = await fetchPlayurlOnce(aid, cid, bvid, qn);
                // durl
                if (d.durl && d.durl.length > 0) {
                    const urls = [];
                    d.durl.forEach(seg => {
                        if (seg.url) urls.push(seg.url);
                        (seg.backup_url || []).forEach(u => { if (u && !urls.includes(u)) urls.push(u); });
                    });
                    if (!urls.length) throw new Error('播放地址为空');
                    return {
                        mode: 'single', urls,
                        qualityId: d.quality || qn,
                        qualityName: qualityLabel(d.quality || qn),
                        downgraded: qn !== quality,
                    };
                }
                // dash
                if (d.dash) {
                    const r = pickDashStreams(d.dash, qn);
                    r.downgraded = qn !== quality;
                    return r;
                }
                throw new Error('不支持的流格式');
            } catch (e) {
                lastErr = e;
                log(`qn=${qn} 失败:`, e.message);
                if (!isQualityError(e) && !/播放数据为空|不支持的流格式/.test(e.message)) {
                    // 非清晰度问题（如网络错误）直接抛出
                    throw e;
                }
            }
        }
        // 全部失败
        const baseMsg = lastErr ? lastErr.message : '所有清晰度均不可用';
        if (/no\s*data/i.test(baseMsg)) {
            throw new Error(
                '该视频在当前清晰度下不可用（可能是大会员专享或视频已限制）。\n' +
                '请尝试：\n' +
                '1. 登录 B 站账号（脚本会自动使用当前页面的登录态）\n' +
                '2. 切换更低的清晰度后重试\n' +
                '3. 刷新页面后重试'
            );
        }
        throw new Error(baseMsg);
    }

    async function fetchBangumiPlayurlOnce(epId, cid, quality) {
        const url = `https://api.bilibili.com/pgc/player/web/playurl?ep_id=${epId}&cid=${cid}` +
                    `&qn=${quality}&fnval=1&fourk=1&otype=json`;
        const data = await fetchJson(url);
        if (data.code !== 0) {
            const err = new Error(data.message || `番剧播放地址获取失败 code=${data.code}`);
            err.code = data.code;
            throw err;
        }
        const d = data.data || data.result;
        if (!d) throw new Error('番剧播放数据为空');
        return d;
    }

    async function getBangumiPlayurl(epId, cid, quality) {
        const fallbacks = [quality, 80, 64, 32, 16]
            .filter((v, i, a) => a.indexOf(v) === i && v <= quality);
        let lastErr = null;
        for (const qn of fallbacks) {
            try {
                log(`番剧尝试清晰度 qn=${qn}`);
                const d = await fetchBangumiPlayurlOnce(epId, cid, qn);
                if (d.durl && d.durl.length > 0) {
                    const urls = [];
                    d.durl.forEach(seg => {
                        if (seg.url) urls.push(seg.url);
                        (seg.backup_url || []).forEach(u => { if (u && !urls.includes(u)) urls.push(u); });
                    });
                    if (!urls.length) throw new Error('播放地址为空');
                    return {
                        mode: 'single', urls,
                        qualityId: d.quality || qn,
                        qualityName: qualityLabel(d.quality || qn),
                        downgraded: qn !== quality,
                    };
                }
                if (d.dash) {
                    const r = pickDashStreams(d.dash, qn);
                    r.downgraded = qn !== quality;
                    return r;
                }
                throw new Error('不支持的流格式');
            } catch (e) {
                lastErr = e;
                log(`番剧 qn=${qn} 失败:`, e.message);
                if (!isQualityError(e) && !/播放数据为空|不支持的流格式/.test(e.message)) throw e;
            }
        }
        const baseMsg = lastErr ? lastErr.message : '所有清晰度均不可用';
        if (/no\s*data/i.test(baseMsg)) {
            throw new Error(
                '番剧在当前清晰度下不可用（需登录或大会员）。\n' +
                '请先扫码/手动登录 B 站，或切换到更低清晰度后重试。'
            );
        }
        throw new Error(baseMsg);
    }

    function pickDashStreams(dash, quality) {
        const videos = dash.video || [];
        const audios = dash.audio || [];
        if (!videos.length) throw new Error('无可用的视频流');
        let candidates = videos.filter(v => v.id === quality);
        if (!candidates.length) {
            const below = videos.filter(v => v.id <= quality);
            candidates = below.length ? below.filter(v => v.id === Math.max(...below.map(v => v.id))) : videos;
        }
        candidates.sort((a, b) => {
            const r = c => (c.codecs || '').startsWith('avc') ? 0 : (c.codecs || '').startsWith('hev') ? 1 : 2;
            return r(a) - r(b);
        });
        const video = candidates[0];
        const audio = audios.length ? audios.reduce((a, b) => (a.bandwidth || 0) > (b.bandwidth || 0) ? a : b) : null;

        const videoUrls = [];
        const vMain = video.baseUrl || video.base_url;
        if (vMain) videoUrls.push(vMain);
        (video.backupUrl || video.backup_url || []).forEach(u => { if (u) videoUrls.push(u); });
        if (!videoUrls.length) throw new Error('视频流地址为空');

        const audioUrls = [];
        if (audio) {
            const aMain = audio.baseUrl || audio.base_url;
            if (aMain) audioUrls.push(aMain);
            (audio.backupUrl || audio.backup_url || []).forEach(u => { if (u) audioUrls.push(u); });
        }

        return {
            mode: 'dash',
            videoUrls, audioUrls,
            videoCodec: video.codecs || '',
            qualityId: video.id,
            qualityName: qualityLabel(video.id),
        };
    }

    // ==================== 下载核心 ====================
    function downloadOne(urls, filename, referer, onProgress) {
        let lastErr = null;
        const tryNext = (i) => {
            if (i >= urls.length) return Promise.reject(lastErr || new Error('所有下载节点均失败'));
            const url = urls[i];
            log(`尝试下载 [${i + 1}/${urls.length}]:`, url.substring(0, 80) + '...');
            return downloadSingle(url, filename, referer, onProgress).catch(e => {
                lastErr = e;
                log(`URL ${i + 1} 失败:`, e.message);
                if (onProgress) onProgress(`节点 ${i + 1} 失败，尝试下一节点...`);
                return tryNext(i + 1);
            });
        };
        return tryNext(0);
    }

    function downloadSingle(url, filename, referer, onProgress) {
        return new Promise((resolve, reject) => {
            GM_xmlhttpRequest({
                method: 'GET',
                url: url,
                headers: {
                    'User-Agent': DEFAULT_UA,
                    'Referer': referer || 'https://www.bilibili.com/',
                    'Origin': 'https://www.bilibili.com',
                    'Accept': '*/*',
                    'Accept-Language': 'zh-CN,zh;q=0.9',
                },
                responseType: 'arraybuffer',
                timeout: 0,
                onprogress: (e) => {
                    if (onProgress && e.lengthComputable && e.total > 0) {
                        onProgress(e.loaded, e.total);
                    }
                },
                onload: (res) => {
                    log('下载响应状态:', res.status, '数据长度:', res.response ? res.response.byteLength : 0);
                    if (res.status < 200 || res.status >= 300) {
                        if (res.status === 403) return reject(new Error(`403 链接已过期，请刷新页面重试`));
                        if (res.status === 404) return reject(new Error(`404 文件不存在`));
                        if (res.status === 0) return reject(new Error(`请求被拦截（请检查 @connect 授权）`));
                        return reject(new Error(`HTTP ${res.status}`));
                    }
                    if (!res.response || res.response.byteLength === 0) {
                        return reject(new Error('下载内容为空（no data）'));
                    }
                    try {
                        const blob = new Blob([res.response], { type: 'video/mp4' });
                        const a = document.createElement('a');
                        a.href = URL.createObjectURL(blob);
                        a.download = filename;
                        a.style.display = 'none';
                        document.body.appendChild(a);
                        a.click();
                        setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 3000);
                        resolve();
                    } catch (e) {
                        reject(new Error('保存文件失败: ' + e.message));
                    }
                },
                onerror: () => reject(new Error(`网络错误（CDN 域名可能未授权）`)),
                ontimeout: () => reject(new Error('下载超时')),
            });
        });
    }

    async function downloadVideo(streams, title, referer, onProgress) {
        const safeTitle = sanitizeFilename(title);
        const ref = referer || 'https://www.bilibili.com/';

        if (streams.mode === 'single') {
            const qnTag = streams.qualityName || '';
            const warn = streams.downgraded ? `（已自动降级至 ${qnTag}）` : `（${qnTag}）`;
            onProgress(`正在下载视频${warn}...`, 0, 0);
            await downloadOne(streams.urls, safeTitle + '.mp4', ref,
                (l, t) => onProgress(`下载中 ${(l / 1048576).toFixed(1)}MB / ${(t / 1048576).toFixed(1)}MB`, l, t));
            return { mode: 'single', title: safeTitle, downgraded: streams.downgraded };
        }

        onProgress('正在下载视频流（该视频仅支持DASH，将分离音视频）...', 0, 40);
        await downloadOne(streams.videoUrls, safeTitle + '_video.m4s', ref,
            (l, t) => onProgress(`视频流 ${(l / 1048576).toFixed(1)}MB`, l, t * 0.4));

        if (streams.audioUrls && streams.audioUrls.length) {
            onProgress('正在下载音频流...', 40, 100);
            await downloadOne(streams.audioUrls, safeTitle + '_audio.m4s', ref,
                (l, t) => onProgress(`音频流 ${(l / 1048576).toFixed(1)}MB`, 40 + l / t * 60, 100));
        }
        return { mode: 'dash', title: safeTitle };
    }

    function sanitizeFilename(name) {
        return (name || 'untitled').replace(/[\\/*?:"<>|\n\r\t]/g, '_').replace(/\s+/g, ' ').trim().slice(0, 120) || 'untitled';
    }

    function escapeHtml(s) {
        return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    // ==================== 专栏 / 动态下载 ====================
    async function downloadArticle(cvId) {
        const articleId = cvId.toLowerCase().startsWith('cv') ? cvId.slice(2) : cvId;
        const data = await fetchJson(`https://api.bilibili.com/x/article/view?id=${articleId}`);
        if (data.code !== 0) throw new Error(data.message || '专栏解析失败');
        const d = data.data || data.result;
        if (!d) throw new Error('专栏数据为空（文章可能已删除或需要登录）');
        const title = d.title || `专栏_${articleId}`;
        const safeTitle = sanitizeFilename(title);
        let contentHtml = d.content || '';
        let imgIndex = 0; const images = [];
        contentHtml = contentHtml.replace(/<img[^>]*>/gi, (tag) => {
            const m = tag.match(/data-src=["']([^"']+)["']/) || tag.match(/src=["']([^"']+)["']/);
            if (!m) return tag;
            let src = m[1];
            if (src.startsWith('//')) src = 'https:' + src;
            else if (src.startsWith('/')) src = 'https://www.bilibili.com' + src;
            else if (!src.startsWith('http')) return tag;
            const name = `img_${String(imgIndex).padStart(3, '0')}.jpg`;
            images.push({ url: src, name }); imgIndex++;
            return tag.replace(/src=["'][^"']*["']/, `src="images/${name}"`);
        });
        for (const img of images) {
            try { await downloadOne([img.url], `${safeTitle}_${img.name}`, `https://www.bilibili.com/read/${cvId}`, () => {}); } catch (e) {}
        }
        const html = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>${escapeHtml(title)}</title>
<style>body{max-width:820px;margin:0 auto;padding:24px;font-family:sans-serif;line-height:1.8;color:#222;}
img{max-width:100%;height:auto;border-radius:6px;} h1{border-bottom:2px solid #00aeec;padding-bottom:10px;}</style></head>
<body><h1>${escapeHtml(title)}</h1><div class="meta">作者：${escapeHtml((d.author && d.author.name) || '')}</div><div class="content">${contentHtml}</div></body></html>`;
        const blob = new Blob([html], { type: 'text/html;charset=utf-8' });
        const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = `${safeTitle}.html`;
        document.body.appendChild(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
        return { title, type: 'article' };
    }

    async function downloadOpus(dynamicId) {
        const url = `https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?timezone_offset=-480&id=${dynamicId}`;
        const data = await fetchJson(url, { Referer: `https://t.bilibili.com/${dynamicId}` });
        if (data.code !== 0) throw new Error(data.message || '动态解析失败');
        const item = data.data && data.data.item;
        if (!item) throw new Error('动态数据为空（动态可能已删除）');
        const modules = item.modules || {};
        const text = (modules.module_dynamic && modules.module_dynamic.desc && modules.module_dynamic.desc.text) || '';
        const major = (modules.module_dynamic && modules.module_dynamic.major) || {};
        if (major.archive && major.archive.bvid) {
            const info = await getVideoInfo(major.archive.bvid);
            const quality = parseInt(document.getElementById('sb-quality')?.value || '80');
            const streams = await getPlayurl(info.aid, info.cid, major.archive.bvid, quality);
            await downloadVideo(streams, info.title, `https://www.bilibili.com/video/${major.archive.bvid}`, () => {});
            return { title: info.title, type: 'video' };
        }
        const images = (major.draw && major.draw.items) ? major.draw.items.map(i => i.src).filter(Boolean) : [];
        const safeTitle = `动态_${dynamicId}`;
        for (let i = 0; i < images.length; i++) {
            try {
                const ext = images[i].toLowerCase().includes('.png') ? '.png' : '.jpg';
                await downloadOne([images[i]], `${safeTitle}_img_${String(i).padStart(3, '0')}${ext}`, `https://t.bilibili.com/${dynamicId}`, () => {});
            } catch (e) {}
        }
        const html = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>${escapeHtml(safeTitle)}</title>
<style>body{max-width:800px;margin:0 auto;padding:20px;font-family:sans-serif;line-height:1.6;}.text{white-space:pre-wrap;padding:16px;background:#f7f7f7;border-radius:8px;}</style></head>
<body><h3>${escapeHtml((modules.module_author && modules.module_author.name) || '未知')}</h3><div class="text">${escapeHtml(text || '（无文字内容）')}</div></body></html>`;
        const blob = new Blob([html], { type: 'text/html;charset=utf-8' });
        const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = `${safeTitle}.html`;
        document.body.appendChild(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
        return { title: safeTitle, type: 'opus' };
    }

    // ==================== UI 样式 ====================
    const PANEL_CSS = `
        #savebili-btn {
            display: inline-flex !important;
            align-items: center !important;
            justify-content: center !important;
            background: #00aeec !important;
            color: #fff !important;
            cursor: pointer !important;
            user-select: none !important;
            white-space: nowrap !important;
            transition: background 0.2s !important;
            box-shadow: 0 2px 6px rgba(0,174,236,0.3) !important;
            vertical-align: middle !important;
            flex-shrink: 0 !important;
            box-sizing: border-box !important;
            font-family: inherit !important;
            margin-left: 8px !important;
            z-index: 100 !important;
            overflow: hidden !important;
            text-overflow: ellipsis !important;
            border-radius: 6px !important;
        }
        #savebili-btn:hover { background: #00c2ff !important; }

        #savebili-btn.sb-floating {
            position: fixed !important;
            bottom: 80px !important; right: 20px !important;
            height: 36px !important; width: auto !important; min-width: 0 !important; max-width: none !important;
            padding: 0 16px !important;
            border-radius: 18px !important; font-size: 13px !important;
            line-height: 36px !important;
            z-index: 99999 !important;
            box-shadow: 0 4px 16px rgba(0,174,236,0.45) !important;
            flex: none !important;
        }

        #savebili-panel {
            position: fixed; width: 340px; max-height: 520px;
            background: linear-gradient(145deg, rgba(30,30,46,0.97), rgba(20,20,35,0.98));
            border: 1px solid rgba(255,255,255,0.12); border-radius: 16px; padding: 16px;
            z-index: 999999; display: none;
            box-shadow: 0 12px 40px rgba(0,0,0,0.55);
            backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
            color: #e6e6f0; font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
            font-size: 13px; overflow-y: auto; scrollbar-width: thin; scrollbar-color: rgba(255,255,255,0.2) transparent;
            box-sizing: border-box;
        }
        #savebili-panel::-webkit-scrollbar { width: 5px; }
        #savebili-panel::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.2); border-radius: 3px; }
        #savebili-panel.show { display: block; animation: sbFadeIn 0.2s ease; }
        @keyframes sbFadeIn { from { opacity:0; transform:translateY(6px); } to { opacity:1; transform:translateY(0); } }

        .sb-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; padding-bottom: 10px; border-bottom: 1px solid rgba(255,255,255,0.08); }
        .sb-header h3 { margin: 0; font-size: 15px; background: linear-gradient(90deg, #00d2ff, #0072ff); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }
        .sb-header .sb-close { cursor: pointer; font-size: 18px; color: #888; width: 26px; height: 26px; display: flex; align-items: center; justify-content: center; border-radius: 50%; transition: all 0.2s; }
        .sb-header .sb-close:hover { background: rgba(255,255,255,0.1); color: #fff; }

        .sb-section { margin-bottom: 12px; }
        .sb-label { font-size: 11px; color: #888; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.5px; }
        .sb-quality-select { width: 100%; padding: 8px 10px; background: rgba(255,255,255,0.07); border: 1px solid rgba(255,255,255,0.12); border-radius: 8px; color: #e6e6f0; font-size: 13px; outline: none; cursor: pointer; transition: border-color 0.2s; }
        .sb-quality-select:focus { border-color: #00aeec; }
        .sb-quality-select option { background: #1e1e2e; color: #e6e6f0; }

        .sb-btns { display: flex; gap: 8px; }
        .sb-btn { flex: 1; padding: 9px 12px; border: none; border-radius: 8px; cursor: pointer; font-size: 13px; font-weight: 500; transition: all 0.2s; color: #fff; background: linear-gradient(135deg, rgba(0,198,255,0.85), rgba(0,114,255,0.85)); box-shadow: 0 3px 10px rgba(0,114,255,0.25); white-space: nowrap; }
        .sb-btn:hover { transform: translateY(-1px); box-shadow: 0 5px 16px rgba(0,174,236,0.35); }
        .sb-btn:active { transform: translateY(0); }
        .sb-btn:disabled { opacity: 0.45; cursor: not-allowed; transform: none; }

        .sb-part-list { max-height: 200px; overflow-y: auto; border: 1px solid rgba(255,255,255,0.08); border-radius: 8px; margin-top: 4px; scrollbar-width: thin; scrollbar-color: rgba(255,255,255,0.15) transparent; }
        .sb-part-list::-webkit-scrollbar { width: 4px; }
        .sb-part-list::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.15); border-radius: 2px; }
        .sb-part-item { display: flex; align-items: center; justify-content: space-between; padding: 7px 10px; cursor: pointer; border-bottom: 1px solid rgba(255,255,255,0.04); transition: background 0.15s; font-size: 12px; }
        .sb-part-item:last-child { border-bottom: none; }
        .sb-part-item:hover { background: rgba(255,255,255,0.06); }
        .sb-part-item.current { background: rgba(0,174,236,0.12); }
        .sb-part-item .sb-part-title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-right: 8px; }
        .sb-part-item .sb-part-dl { font-size: 11px; padding: 3px 10px; border-radius: 5px; background: rgba(0,174,236,0.2); color: #00d2ff; cursor: pointer; white-space: nowrap; transition: all 0.15s; border: none; }
        .sb-part-item .sb-part-dl:hover { background: rgba(0,174,236,0.4); }

        .sb-status { margin-top: 10px; padding: 8px 10px; background: rgba(0,0,0,0.25); border-radius: 8px; font-size: 12px; color: #aaa; line-height: 1.5; white-space: pre-wrap; word-break: break-all; min-height: 20px; max-height: 160px; overflow-y: auto; display: none; }
        .sb-status.show { display: block; }
        .sb-status.warn { color: #ffb84d; }

        .sb-progress { width: 100%; height: 4px; background: rgba(255,255,255,0.08); border-radius: 2px; margin-top: 8px; overflow: hidden; display: none; }
        .sb-progress.show { display: block; }
        .sb-progress-bar { height: 100%; width: 0%; background: linear-gradient(90deg, #00d2ff, #0072ff); border-radius: 2px; transition: width 0.3s; }
    `;

    function injectStyles() {
        if (document.getElementById('savebili-style')) return;
        const style = document.createElement('style');
        style.id = 'savebili-style';
        style.textContent = PANEL_CSS;
        document.head.appendChild(style);
    }

    function createPanel() {
        if (document.getElementById('savebili-panel')) return;
        const panel = document.createElement('div');
        panel.id = 'savebili-panel';
        panel.innerHTML = `
            <div class="sb-header">
                <h3>SaveBili 下载助手</h3>
                <span class="sb-close" id="sb-close">×</span>
            </div>
            <div class="sb-section">
                <div class="sb-label">清晰度选择</div>
                <select class="sb-quality-select" id="sb-quality">
                    <option value="120">超清 4K (120)</option>
                    <option value="116">高清 1080P60 (116)</option>
                    <option value="112">高清 1080P+ (112)</option>
                    <option value="80" selected>高清 1080P (80)</option>
                    <option value="74">高清 720P60 (74)</option>
                    <option value="64">高清 720P (64)</option>
                    <option value="32">清晰 480P (32)</option>
                    <option value="16">流畅 360P (16)</option>
                </select>
            </div>
            <div class="sb-section" id="sb-actions"></div>
            <div class="sb-section" id="sb-parts" style="display:none;">
                <div class="sb-label">分P / 剧集列表</div>
                <div class="sb-part-list" id="sb-part-list"></div>
            </div>
            <div class="sb-status" id="sb-status"></div>
            <div class="sb-progress" id="sb-progress">
                <div class="sb-progress-bar" id="sb-progress-bar"></div>
            </div>
        `;
        document.body.appendChild(panel);

        const savedQuality = GM_getValue('sb_quality', '80');
        panel.querySelector('#sb-quality').value = savedQuality;
        panel.querySelector('#sb-quality').addEventListener('change', (e) => { GM_setValue('sb_quality', e.target.value); });
        panel.querySelector('#sb-close').addEventListener('click', () => {
            panel.classList.remove('show');
            document.getElementById('savebili-btn')?.classList.remove('active');
        });
    }

    // ==================== 按钮注入与尺寸同步 ====================
    function findSendButton() {
        const selectors = [
            '.bpx-player-video-btn-send',
            '.bpx-player-video-inputbar .bpx-player-video-btn-send',
            '.bpx-player-dm-btn-send',
            '.bilibili-player-video-btn-send'
        ];
        for (const sel of selectors) {
            const el = document.querySelector(sel);
            if (el) return el;
        }
        return null;
    }

    function syncButtonStyle(btn, sendBtn) {
        if (!btn || !sendBtn) return;
        const sendStyle = window.getComputedStyle(sendBtn);
        btn.style.height = sendStyle.height;
        btn.style.lineHeight = sendStyle.lineHeight;
        btn.style.fontSize = sendStyle.fontSize;
        btn.style.padding = sendStyle.padding;
        btn.style.fontWeight = sendStyle.fontWeight;
        const sendW = sendBtn.offsetWidth;
        if (sendW > 0) {
            btn.style.width = sendW + 'px';
            btn.style.minWidth = sendW + 'px';
            btn.style.maxWidth = sendW + 'px';
            btn.style.flex = '0 0 ' + sendW + 'px';
        }
        const sendH = sendBtn.offsetHeight;
        if (sendH > 0) {
            btn.style.height = sendH + 'px';
            btn.style.lineHeight = sendH + 'px';
        }
    }

    function createDownloadButton() {
        const btn = document.createElement('div');
        btn.id = 'savebili-btn';
        btn.textContent = '下载';
        btn.title = 'SaveBili 下载助手';
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            e.preventDefault();
            togglePanel(btn);
        });
        return btn;
    }

    // ==================== 面板定位 ====================
    function positionPanel(panel, btn) {
        const rect = btn.getBoundingClientRect();
        const panelW = 340;
        const maxH = 520;
        let left = rect.right - panelW;
        if (left < 10) left = 10;
        if (left + panelW > window.innerWidth - 10) left = window.innerWidth - panelW - 10;
        const spaceBelow = window.innerHeight - rect.bottom;
        const spaceAbove = rect.top;
        let top = (spaceBelow >= maxH + 16 || spaceBelow >= spaceAbove) ? rect.bottom + 8 : Math.max(10, rect.top - maxH - 8);
        panel.style.left = left + 'px';
        panel.style.top = top + 'px';
        panel.style.right = 'auto';
        panel.style.bottom = 'auto';
    }

    function togglePanel(btn) {
        const panel = document.getElementById('savebili-panel');
        if (!panel) return;
        if (panel.classList.contains('show')) {
            panel.classList.remove('show');
            btn.classList.remove('active');
            return;
        }
        positionPanel(panel, btn);
        panel.classList.add('show');
        btn.classList.add('active');
        renderPanelContent();
    }

    // ==================== 状态更新 ====================
    function updatePanelStatus(msg, showProgress, percent, isWarn) {
        const statusEl = document.getElementById('sb-status');
        const progressEl = document.getElementById('sb-progress');
        const barEl = document.getElementById('sb-progress-bar');
        if (statusEl) {
            statusEl.textContent = msg || '';
            statusEl.classList.toggle('show', !!msg);
            statusEl.classList.toggle('warn', !!isWarn);
        }
        if (progressEl) {
            progressEl.classList.toggle('show', !!showProgress);
            if (barEl && percent !== undefined) barEl.style.width = Math.min(100, Math.max(0, percent)) + '%';
        }
    }

    // ==================== 渲染面板内容 ====================
    async function renderPanelContent() {
        const pageInfo = getPageInfo();
        if (!pageInfo) return;
        const actionsEl = document.getElementById('sb-actions');
        const partsEl = document.getElementById('sb-parts');
        const partListEl = document.getElementById('sb-part-list');
        if (!actionsEl) return;

        actionsEl.innerHTML = ''; partsEl.style.display = 'none'; partListEl.innerHTML = '';

        if (pageInfo.type === 'video') {
            actionsEl.innerHTML = `<div class="sb-btns"><button class="sb-btn" id="sb-dl-current">下载当前视频</button></div>`;
            try {
                updatePanelStatus('正在获取视频信息...');
                const info = await getVideoInfo(pageInfo.id);
                updatePanelStatus('');
                if (info.pages.length > 1) {
                    partsEl.style.display = 'block';
                    partListEl.innerHTML = info.pages.map((p) => {
                        const isCurrent = String(p.page) === (new URLSearchParams(location.search).get('p') || '1');
                        return `<div class="sb-part-item ${isCurrent ? 'current' : ''}"><span class="sb-part-title">${escapeHtml(p.part || `P${p.page}`)}</span><button class="sb-part-dl" data-cid="${p.cid}">下载</button></div>`;
                    }).join('');
                    partListEl.querySelectorAll('.sb-part-dl').forEach(b => {
                        b.addEventListener('click', async (e) => {
                            e.stopPropagation();
                            const cid = parseInt(b.dataset.cid);
                            const page = info.pages.find(p => p.cid === cid);
                            await downloadVideoPart(pageInfo.id, info, cid, page?.part);
                        });
                    });
                    document.getElementById('sb-dl-current').textContent = '下载当前P';
                }
                document.getElementById('sb-dl-current').onclick = async () => {
                    const currentP = new URLSearchParams(location.search).get('p') || '1';
                    const page = info.pages.find(p => String(p.page) === currentP) || info.pages[0];
                    await downloadVideoPart(pageInfo.id, info, page.cid, page.part);
                };
            } catch (e) { updatePanelStatus('获取视频信息失败: ' + e.message, false, 0, true); }
        } else if (pageInfo.type === 'bangumi') {
            actionsEl.innerHTML = `<div class="sb-btns"><button class="sb-btn" id="sb-dl-current">下载当前集</button></div>`;
            try {
                updatePanelStatus('正在获取番剧信息...');
                const info = await getBangumiInfo(pageInfo.id);
                updatePanelStatus('');
                if (info.episodes.length > 0) {
                    partsEl.style.display = 'block';
                    const currentEpNum = pageInfo.id.replace(/[a-z]/gi, '');
                    partListEl.innerHTML = info.episodes.map((ep) => {
                        const isCurrent = String(ep.ep_id) === currentEpNum;
                        return `<div class="sb-part-item ${isCurrent ? 'current' : ''}"><span class="sb-part-title">${escapeHtml(ep.index)} ${escapeHtml(ep.title)}</span><button class="sb-part-dl" data-ep="${ep.ep_id}" data-cid="${ep.cid}">下载</button></div>`;
                    }).join('');
                    partListEl.querySelectorAll('.sb-part-dl').forEach(b => {
                        b.addEventListener('click', async (e) => { e.stopPropagation(); await downloadBangumiEp(parseInt(b.dataset.ep), parseInt(b.dataset.cid), info.title); });
                    });
                }
                document.getElementById('sb-dl-current').onclick = async () => {
                    const epNum = pageInfo.id.startsWith('ep') ? parseInt(pageInfo.id.slice(2)) : null;
                    const ep = info.episodes.find(e => e.ep_id === epNum) || info.episodes[0];
                    if (ep) await downloadBangumiEp(ep.ep_id, ep.cid, info.title);
                };
            } catch (e) { updatePanelStatus('番剧信息获取失败: ' + e.message, false, 0, true); }
        } else if (pageInfo.type === 'article') {
            actionsEl.innerHTML = `<div class="sb-btns"><button class="sb-btn" id="sb-dl-article">下载专栏</button></div>`;
            document.getElementById('sb-dl-article').onclick = async () => {
                try { updatePanelStatus('正在解析专栏...'); const r = await downloadArticle(pageInfo.id); updatePanelStatus(`专栏「${r.title}」下载完成`); } catch (e) { updatePanelStatus('专栏下载失败: ' + e.message, false, 0, true); }
            };
        } else if (pageInfo.type === 'opus') {
            actionsEl.innerHTML = `<div class="sb-btns"><button class="sb-btn" id="sb-dl-opus">下载动态</button></div>`;
            document.getElementById('sb-dl-opus').onclick = async () => {
                try { updatePanelStatus('正在解析动态...'); const r = await downloadOpus(pageInfo.id); updatePanelStatus(`动态「${r.title}」下载完成`); } catch (e) { updatePanelStatus('动态下载失败: ' + e.message, false, 0, true); }
            };
        } else if (pageInfo.type === 'space') {
            actionsEl.innerHTML = `<div style="color:#888;text-align:center;padding:8px;">用户空间页面，请进入具体视频/动态页面下载</div>`;
        }
    }

    // ==================== 执行下载 ====================
    async function downloadVideoPart(bvid, info, cid, partTitle) {
        try {
            const quality = parseInt(document.getElementById('sb-quality').value);
            const page = info.pages.find(p => p.cid === cid) || info.pages[0];
            const title = info.pages.length > 1 ? `${info.title} - ${partTitle || page.part || 'P' + page.page}` : info.title;
            updatePanelStatus('正在获取播放地址...');
            const streams = await getPlayurl(info.aid, cid, bvid, quality);
            const result = await downloadVideo(streams, title, `https://www.bilibili.com/video/${bvid}`,
                (msg, l, t) => { const pct = t > 0 ? Math.round(l / t * 100) : 0; updatePanelStatus(msg, true, pct); });

            if (result.mode === 'single') {
                const tag = result.downgraded ? '（已降级清晰度）' : '';
                updatePanelStatus(`「${title}」下载完成 ${tag}`);
            } else {
                updatePanelStatus(`「${title}」音视频已分别下载。\n\n该视频仅提供DASH分离格式（m4s），需要用 FFmpeg 合并后才能播放：\nffmpeg -i "${result.title}_video.m4s" -i "${result.title}_audio.m4s" -c copy "${result.title}.mp4"`, false, 100, true);
            }
        } catch (e) { updatePanelStatus('下载失败: ' + e.message, false, 0, true); console.error('[SaveBili]', e); }
    }

    async function downloadBangumiEp(epId, cid, bangumiTitle) {
        try {
            const quality = parseInt(document.getElementById('sb-quality').value);
            updatePanelStatus('正在获取番剧播放地址...');
            const streams = await getBangumiPlayurl(epId, cid, quality);
            const title = `${bangumiTitle}_ep${epId}`;
            const result = await downloadVideo(streams, title, location.href,
                (msg, l, t) => { const pct = t > 0 ? Math.round(l / t * 100) : 0; updatePanelStatus(msg, true, pct); });
            if (result.mode === 'single') {
                const tag = result.downgraded ? '（已降级清晰度）' : '';
                updatePanelStatus(`「${title}」下载完成 ${tag}`);
            } else {
                updatePanelStatus(`「${title}」音视频已分别下载，请用 FFmpeg 合并。`, false, 100, true);
            }
        } catch (e) { updatePanelStatus('番剧下载失败: ' + e.message, false, 0, true); console.error('[SaveBili]', e); }
    }

    // ==================== 核心自愈逻辑 ====================
    let lastUrl = location.href;

    function ensureButton() {
        const btn = document.getElementById('savebili-btn');
        const sendBtn = findSendButton();
        const pageInfo = getPageInfo();

        if (!pageInfo) { if (btn) btn.remove(); return; }

        const isVideoOrBangumi = pageInfo.type === 'video' || pageInfo.type === 'bangumi';

        if (isVideoOrBangumi) {
            if (sendBtn) {
                if (!btn || btn.previousSibling !== sendBtn) {
                    if (btn) btn.remove();
                    const newBtn = createDownloadButton();
                    sendBtn.parentNode.insertBefore(newBtn, sendBtn.nextSibling);
                    syncButtonStyle(newBtn, sendBtn);
                } else {
                    syncButtonStyle(btn, sendBtn);
                }
            } else {
                if (btn && !btn.classList.contains('sb-floating')) btn.remove();
            }
        } else {
            if (!btn) {
                const newBtn = createDownloadButton();
                newBtn.classList.add('sb-floating');
                newBtn.textContent = '下载';
                document.body.appendChild(newBtn);
            } else if (!btn.classList.contains('sb-floating')) {
                btn.classList.add('sb-floating');
                btn.textContent = '下载';
                btn.style.width = ''; btn.style.minWidth = ''; btn.style.maxWidth = '';
                btn.style.height = ''; btn.style.lineHeight = ''; btn.style.fontSize = '';
                btn.style.padding = ''; btn.style.fontWeight = '';
                btn.style.flex = '';
                document.body.appendChild(btn);
            }
        }
    }

    function init() {
        injectStyles();
        createPanel();
        ensureButton();

        const observer = new MutationObserver(() => { ensureButton(); });
        observer.observe(document.body, { childList: true, subtree: true });

        setInterval(ensureButton, 1000);

        setInterval(() => {
            if (location.href !== lastUrl) {
                lastUrl = location.href;
                document.getElementById('savebili-panel')?.classList.remove('show');
                document.getElementById('savebili-btn')?.classList.remove('active');
                setTimeout(ensureButton, 300);
                setTimeout(ensureButton, 1000);
            }
        }, 500);

        document.addEventListener('click', (e) => {
            const panel = document.getElementById('savebili-panel');
            const btn = document.getElementById('savebili-btn');
            if (!panel || !panel.classList.contains('show')) return;
            if (panel.contains(e.target)) return;
            if (btn && btn.contains(e.target)) return;
            panel.classList.remove('show');
            btn?.classList.remove('active');
        });

        window.addEventListener('resize', () => {
            const panel = document.getElementById('savebili-panel');
            const btn = document.getElementById('savebili-btn');
            if (panel?.classList.contains('show') && btn) positionPanel(panel, btn);
        });
    }

    if (document.readyState === 'complete' || document.readyState === 'interactive') init();
    else window.addEventListener('DOMContentLoaded', init);

})();