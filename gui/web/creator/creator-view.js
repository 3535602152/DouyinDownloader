/* Creator homepage, work cards and the video / 图文 player, all project-authored markup. */
(function (global) {
  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, char => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[char]))
  }
  function text(node, value) {
    if (node) node.textContent = value == null ? '' : String(value)
  }
  function setImg(node, src, alt) {
    if (!node) return
    if (src) node.setAttribute('src', src)
    else node.removeAttribute('src')
    if (alt != null) node.setAttribute('alt', alt)
  }
  // Cut a signature to `limit` UTF-16 units without splitting an emoji or a
  // combining sequence, so the preview never ends on a broken glyph.
  function truncateText(value, limit) {
    const textValue = String(value ?? '')
    if (textValue.length <= limit) return textValue
    const chars = typeof Intl !== 'undefined' && Intl.Segmenter
      ? Array.from(new Intl.Segmenter('zh', { granularity: 'grapheme' }).segment(textValue), part => part.segment)
      : Array.from(textValue)
    let length = 0
    for (let index = 0; index < chars.length; index += 1) {
      length += chars[index].length
      if (length > limit) return chars.slice(0, index).join('') + '...'
    }
    return textValue
  }
  function genderLabel(value) {
    const gender = Number(value || 0)
    return gender === 1 ? '男' : gender === 2 ? '女' : ''
  }

  const svg = (body, size = 24, extra = '') =>
    '<svg width="' + size + '" height="' + size + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"' + extra + '>' + body + '</svg>'
  const fill = (path, viewBox, size = 30) =>
    '<svg width="' + size + '" height="' + size + '" viewBox="' + viewBox + '" aria-hidden="true"><path fill="currentColor" fill-rule="evenodd" clip-rule="evenodd" d="' + path + '"/></svg>'
  const ICONS = {
    back: svg('<path d="M15 18l-6-6 6-6"/>', 16),
    search: svg('<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>', 16),
    calendar: svg('<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>', 16),
    // Card like count: the web homepage's own outlined heart (ic_basic_like).
    heart: '<svg width="16" height="16" viewBox="0 0 17 17" aria-hidden="true"><path fill="currentColor" fill-rule="evenodd" clip-rule="evenodd" d="M5.615 3.282A2.868 2.868 0 0 0 2.75 6.275c0 .03 0 .059-.002.088.015.519.233 1.12.637 1.781.414.678.99 1.363 1.625 2.008 1.214 1.237 2.577 2.265 3.233 2.734.655-.47 2.018-1.497 3.233-2.733.633-.646 1.21-1.331 1.624-2.009.404-.66.622-1.262.637-1.781a.689.689 0 0 1-.002-.088A2.868 2.868 0 0 0 8.75 4.218a.687.687 0 0 1-1.015 0 2.859 2.859 0 0 0-2.12-.936zm9.496 2.978l.001.045c0 .888-.356 1.764-.84 2.556-.488.798-1.141 1.567-1.816 2.254-1.350 1.375-2.855 2.492-3.499 2.949-.43.306-.998.306-1.429 0-.644-.457-2.149-1.574-3.499-2.949-.675-.687-1.328-1.456-1.816-2.254-.484-.792-.84-1.668-.84-2.556l.001-.045-.001-.11a4.242 4.242 0 0 1 6.870-3.330 4.242 4.242 0 0 1 6.868 3.440z"/></svg>',
    // Rail icons: the web player's own like / comment / collect / share glyphs.
    // Heart and bubble are drawn on a 24 grid, star and arrow on a 36 grid; the
    // view boxes below give all four the web player's relative size in a 30px slot.
    heartFill: fill('M2.00534 9C2.00179 8.91711 2 8.83376 2 8.75C2 5.57436 4.57436 3 7.75 3C9.43372 3 10.9484 3.72368 12 4.87696C13.0516 3.72368 14.5663 3 16.25 3C19.4256 3 22 5.57436 22 8.75C22 8.83376 21.9982 8.91711 21.9947 9H22C22 13.5738 14.6263 19.141 12.5425 20.6229C12.2133 20.8571 11.7867 20.8571 11.4575 20.6229C9.37369 19.141 2 13.5738 2 9H2.00534Z', '-1 -1 26 26'),
    comment: fill('M18.8571 17.3202C20.8406 15.3333 22 13.2638 22 10.8541C22 5.96406 17.5232 2 12 2C6.47686 2 2 5.96406 2 10.8542C2 16.8002 8 19.2002 11.429 19.2002V20.7211C11.429 22.3041 12.3493 22.1732 13.1196 21.7347C14.586 20.9 17.4106 18.7693 18.8571 17.3202ZM8.57142 11.0778C8.57142 11.8619 7.93154 12.4971 7.1422 12.4971C6.35418 12.4971 5.71427 11.8619 5.71427 11.0778C5.71427 10.2953 6.35418 9.66007 7.1422 9.66007C7.93154 9.66007 8.57142 10.2953 8.57142 11.0778ZM12 12.4971C11.2112 12.4971 10.5715 11.8619 10.5715 11.0778C10.5715 10.2953 11.2111 9.66007 12 9.66007C12.789 9.66007 13.4286 10.2953 13.4286 11.0778C13.4286 11.8619 12.789 12.4971 12 12.4971ZM18.2857 11.0778C18.2857 11.8619 17.6467 12.4971 16.8575 12.4971C16.0683 12.4971 15.4284 11.8619 15.4285 11.0778C15.4285 10.2953 16.0683 9.66007 16.8575 9.66007C17.6467 9.66007 18.2857 10.2953 18.2857 11.0778Z', '-1 -1 26 26'),
    star: fill('M16.5101 7.2579C17.0254 5.84046 18.9746 5.84047 19.4899 7.2579L21.8225 13.6744L28.4762 13.9734C29.946 14.0395 30.5484 15.9463 29.397 16.8884L24.1849 21.153L25.9645 27.7544C26.3577 29.2126 24.7807 30.3911 23.5538 29.5559L18 25.7751L12.4462 29.5559C11.2193 30.3911 9.64234 29.2126 10.0355 27.7544L11.8151 21.153L6.60299 16.8884C5.45162 15.9463 6.05397 14.0395 7.5238 13.9734L14.1775 13.6744L16.5101 7.2579Z', '3 3 30 30'),
    share: fill('M21.8839 9.41656C20.675 8.26037 18.67 9.11717 18.67 10.7899V13.186C18.5357 13.186 18.4029 13.1872 18.2714 13.1895C16.8626 13.1517 12.0185 13.3097 8.61024 16.9648C6.24883 19.4974 5.18753 23.5267 5.25283 25.606C5.19102 27.6796 6.15808 27.4932 6.41822 27.0157C9.39378 21.554 18.67 23.2251 18.67 23.2251V25.4897C18.67 27.1268 20.6019 27.9978 21.8286 26.9138L29.8176 19.855C30.6512 19.1185 30.6767 17.8265 29.8728 17.0576L21.8839 9.41656Z', '3 3 30 30'),
    gallery: svg('<rect x="7" y="3" width="14" height="14" rx="2"/><path d="M3 7v12a2 2 0 0 0 2 2h12"/>', 14),
    offline: svg('<path d="M3 3l18 18"/><path d="M10.6 5.1A9.8 9.8 0 0 1 12 5c5 0 9 4.5 10 7a13 13 0 0 1-2.6 3.7"/>' +
      '<path d="M6.6 6.6C4.3 8 2.7 10.2 2 12c1 2.5 5 7 10 7a9.6 9.6 0 0 0 5.4-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>', 14),
    close: svg('<path d="M6 6l12 12M18 6L6 18"/>', 20, ' stroke-width="2.4"'),
    up: svg('<path d="M6 15l6-6 6 6"/>', 22),
    down: svg('<path d="M6 9l6 6 6-6"/>', 22),
    left: svg('<path d="M15 6l-6 6 6 6"/>', 22),
    right: svg('<path d="M9 6l6 6-6 6"/>', 22),
    play: svg('<path d="M7 4.5v15l13-7.5z" fill="currentColor" stroke="none"/>', 20),
    pause: svg('<path d="M7 4h3.5v16H7zM13.5 4H17v16h-3.5z" fill="currentColor" stroke="none"/>', 20),
    bigPlay: svg('<path d="M7 4.5v15l13-7.5z" fill="currentColor" stroke="none"/>', 64),
    volume: svg('<path d="M4 9h4l5-4v14l-5-4H4z" fill="currentColor"/><path d="M16.5 8.5a5 5 0 0 1 0 7M19 6a8.5 8.5 0 0 1 0 12"/>', 20),
    muted: svg('<path d="M4 9h4l5-4v14l-5-4H4z" fill="currentColor"/><path d="M17 9l5 6M22 9l-5 6"/>', 20),
    fullscreen: svg('<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>', 20),
  }

  // ── Homepage ───────────────────────────────────────────────────────────

  // Images fade in once decoded. Sources that already loaded render visible
  // straight away, so a re-render never flashes them.
  const loadedImages = new Set()
  function fadeImg(className, src, alt, extra = '') {
    return '<img class="' + className + ' cv-fade' + (loadedImages.has(src) ? ' is-loaded' : '') + '" src="' + esc(src) + '" alt="' + esc(alt) + '"' + extra + '>'
  }
  document.addEventListener('load', event => {
    const image = event.target
    if (!(image instanceof HTMLImageElement) || !image.classList.contains('cv-fade')) return
    loadedImages.add(image.getAttribute('src') || '')
    image.classList.add('is-loaded')
  }, true)
  // A broken image still has to leave its skeleton.
  document.addEventListener('error', event => {
    const image = event.target
    if (image instanceof HTMLImageElement && image.classList.contains('cv-fade')) image.classList.add('is-loaded')
  }, true)

  const GENDER_ICONS = {
    1: '<svg class="cv-gender is-male" width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-label="男"><circle cx="5" cy="7" r="3.25"/><path d="M7.4 4.6L10.5 1.5M7.5 1.5h3v3"/></svg>',
    2: '<svg class="cv-gender is-female" width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-label="女"><circle cx="6" cy="4.5" r="3.25"/><path d="M6 7.75V11.5M4 9.75h4"/></svg>',
  }
  const skel = width => '<span class="cv-skel" style="width:' + width + '"></span>'

  // Every row keeps its final height whether or not the profile has arrived,
  // so nothing below moves when the data lands.
  function profileHtml(creator, streamer) {
    const pending = !!streamer.profileLoading && !streamer.profileKnown
    const nickname = String(creator.nickname || '')
    const stat = (label, value) => '<span class="cv-stat"><em>' + label + '</em><strong>' +
      (streamer.profileKnown ? esc(global.streamerCount(value)) : pending ? skel('44px') : '—') + '</strong></span>'
    const stats = stat('关注', creator.following_count) + stat('粉丝', creator.follower_count) + stat('获赞', creator.total_favorited)
    // Plain text for the account number and IP location, small tags for
    // gender / age and the home region, like the web homepage.
    const texts = [
      creator.unique_id ? '抖音号：' + creator.unique_id : '',
      // Some sources already carry the "IP属地：" label.
      creator.ip_location ? 'IP属地：' + String(creator.ip_location).replace(/^\s*IP属地[：:]\s*/, '') : '',
    ].filter(Boolean).map(item => '<span class="cv-meta-text">' + esc(item) + '</span>')
    const gender = Number(creator.gender || 0)
    const age = Number(creator.age || 0)
    const genderTag = gender === 1 || gender === 2 || age > 0
      ? '<span class="cv-tag">' + (gender === 1 || gender === 2 ? GENDER_ICONS[gender] : '') +
        (age > 0 ? esc(age + '岁') : esc(genderLabel(gender))) + '</span>' : ''
    const region = [creator.province, creator.city].map(value => String(value || '').trim()).filter(Boolean)
    const country = String(creator.country || '').trim()
    const regionLabel = region.length ? region.join(' · ') : country && country !== '中国' ? country : ''
    const regionTag = regionLabel ? '<span class="cv-tag">' + esc(regionLabel) + '</span>' : ''
    const metaItems = texts.concat([genderTag, regionTag].filter(Boolean))
    const meta = metaItems.length ? metaItems.join('') : pending ? skel('220px') : ''
    const rawSignature = String(creator.signature || '').trim()
    const signature = rawSignature || '暂无简介'
    const preview = truncateText(signature.split('\n').join(' '), 30)
    const full = preview === signature ? ''
      : '<div class="cv-bio-full" role="tooltip">' + signature.split('\n').map(esc).join('<br>') + '</div>'
    const bio = !rawSignature && pending ? skel('320px')
      : '<span>' + esc(preview) + '</span>' + (full ? '<span class="cv-bio-more">更多</span>' + full : '')
    const coverUrl = String(creator.cover_url || '').replace(/"/g, '')
    const cover = coverUrl
      ? '<span class="cv-cover-strip">' + fadeImg('cv-cover-image', coverUrl, '') + '</span>'
      : ''
    const avatar = creator.avatar_url
      ? '<span class="cv-avatar">' + fadeImg('', creator.avatar_url, nickname + '头像') + '</span>'
      : '<span class="cv-avatar' + (nickname ? '' : ' cv-skel-block') + '" aria-hidden="true">' + esc(nickname.slice(0, 1)) + '</span>'
    return '<header class="cv-profile' + (cover ? ' has-cover' : '') + '">' +
      '<div class="cv-cover" aria-hidden="true">' + cover + '</div>' +
      '<div class="cv-profile-body">' + avatar +
        '<div class="cv-profile-info">' +
          '<h1 class="cv-name">' + (nickname ? esc(nickname) : streamer.loading || pending ? skel('160px') : '未知主播') + '</h1>' +
          '<div class="cv-stats">' + stats + '</div>' +
          '<div class="cv-meta">' + meta + '</div>' +
          '<div class="cv-bio' + (full && !bio.startsWith('<span class="cv-skel') ? ' has-more' : '') + '"' + (full ? ' tabindex="0"' : '') + '>' + bio + '</div>' +
        '</div>' +
      '</div>' +
    '</header>'
  }

  function skeletonCards(count) {
    return Array.from({ length: count }, () =>
      '<li class="cv-card cv-card-skeleton" aria-hidden="true"><div class="cv-card-thumb cv-skel-block"></div>' +
      '<p class="cv-card-title">' + skel('90%') + skel('60%') + '</p></li>').join('')
  }

  function tab(label, attrs, selected, extra = '') {
    return '<button type="button" role="tab" class="cv-tab' + (selected ? ' is-active' : '') + '" aria-selected="' + selected + '"' +
      ' tabindex="0" ' + attrs + '>' + esc(label) + extra + '</button>'
  }
  function pill(label, attrs, selected) {
    return '<button type="button" role="tab" class="cv-pill' + (selected ? ' is-active' : '') + '" aria-selected="' + selected + '"' +
      ' tabindex="0" ' + attrs + '>' + esc(label) + '</button>'
  }

  // A followed creator shows 作品 with an 已隐藏 filter; the signed-in account's
  // own home switches between 作品 / 喜欢 / 收藏 like the downloader's modes.
  function navigationHtml(creator, streamer) {
    const catalogue = streamer.catalogue
    const workCount = Number(creator.aweme_count || 0) || (streamer.works || []).length
    const countTag = '<span class="cv-tab-count">' + (streamer.loading && !workCount ? skel('24px') : esc(workCount)) + '</span>'
    let primary
    let secondary
    let searchLabel = '搜索 Ta 的作品'
    let showDate = true
    if (!catalogue) {
      const subtab = streamer.subtab === 'hidden' ? 'hidden' : 'works'
      primary = tab('作品', 'aria-controls="cv-work-list"', true, countTag)
      secondary = pill('作品', 'data-action="streamer-subtab" data-tab="works"', subtab === 'works') +
        pill('已隐藏', 'data-action="streamer-subtab" data-tab="hidden"', subtab === 'hidden')
    } else {
      const section = streamer.homeSection || streamer.contentMode || 'post'
      const mode = (key, label, action, extra = '') =>
        tab(label, 'aria-controls="cv-work-list" data-action="' + action + '" data-mode="' + key + '"', section === key, extra)
      primary = mode('post', '作品', 'account-home-mode', countTag) + mode('like', '喜欢', 'account-home-mode') +
        mode('favorite_collection', '收藏', 'account-home-section')
      const sub = (key, label, selected) => pill(label, 'data-action="account-home-mode" data-mode="' + key + '"', selected)
      secondary = section === 'favorite_collection' ? sub('collect', '视频', false) + sub('collectmix', '合集', false)
        : section === 'post' ? sub('post', '视频', true) + sub('mix', '合集', false) : ''
      searchLabel = section === 'like' ? '搜索你赞过的作品' : section === 'favorite_collection' ? '搜索你收藏的作品' : '搜索你发布的作品'
      showDate = section === 'post'
    }
    const recent = streamer.date === 'recent'
    const date = showDate
      ? '<button type="button" class="cv-filter-button' + (recent ? ' is-active' : '') + '" data-action="streamer-date-toggle" aria-pressed="' + recent + '" title="只看最近 30 天发布的作品">' +
        ICONS.calendar + '<span>最近 30 天</span></button>'
      : ''
    return '<div class="cv-tabs" role="tablist" aria-label="主页栏目">' + primary + '</div>' +
      '<div class="cv-filter-row">' +
        (secondary ? '<div class="cv-pills" role="tablist" aria-label="作品分类">' + secondary + '</div>' : '') +
        '<span class="cv-spacer"></span>' +
        '<label class="cv-search">' + ICONS.search + '<input type="search" data-bind="streamerSearch" value="' + esc(streamer.search || '') +
          '" placeholder="' + esc(searchLabel) + '" aria-label="' + esc(searchLabel) + '"></label>' + date +
      '</div>'
  }

  function catalogueFooter(streamer) {
    const catalogue = streamer.catalogue
    const label = catalogue.mode === 'like' ? '喜欢' : '作品'
    const count = catalogue.works.length
    if (catalogue.scanState === 'failed') return '读取未完成：' + (catalogue.error || '请重试') + ' · 已保留 ' + count + ' 个' + label
    if (catalogue.scanState === 'cancelled') return '已停止读取 · 已保留 ' + count + ' 个' + label
    return '已发现 ' + count + ' 个' + label + (catalogue.scanState === 'loading' ? ' · 正在逐页读取…' : ' · 已读取当前可见内容')
  }

  function syncCatalogue(changedIds) {
    const streamer = global.state.streamer
    if (!streamer.catalogue) return false
    if (streamer.homeSection === 'favorite_collection') return true
    const host = document.querySelector('#content [data-creator-home]')
    const anchor = host && host.querySelector('[data-catalogue-tail]')
    if (!anchor || anchor.getAttribute('data-request-id') !== streamer.catalogue.requestId) return false
    const works = global.streamerFilteredWorks()
    if (changedIds === undefined) {
      global.AccountCatalogue.appendCards(anchor, works, work => renderCard(work, streamer.works.indexOf(work), streamer.secUid))
      host._catalogueCards = null
    }
    if (!host._catalogueCards) host._catalogueCards = new Map(Array.from(host.querySelectorAll('[data-catalogue-card]'), card => [card.getAttribute('data-catalogue-card'), card]))
    const empty = host.querySelector('[data-catalogue-empty]')
    if (empty && works.length) empty.remove()
    else if (empty) {
      const loading = streamer.catalogue.scanState === 'loading'
      // The first-load skeleton becomes the plain empty message once reading stops.
      if (!loading && empty.classList.contains('cv-skeleton-group')) {
        empty.className = 'cv-empty-row'
        empty.innerHTML = '<div class="cv-empty" role="status"></div>'
      }
      text(empty.querySelector('.cv-empty'), loading ? '' : streamer.search || streamer.date ? '暂无符合条件的内容' : '当前栏目暂无可见内容')
    }
    ;(changedIds || []).forEach(id => {
      const card = host._catalogueCards.get(String(id))
      const work = streamer.catalogue.byId.get(String(id))
      const badge = card && card.querySelector('[data-catalogue-status]')
      if (!work || !badge) return
      const status = work.download_status || 'unknown'
      text(badge, global.AccountCatalogue.labels[status] || '状态未知')
      badge.setAttribute('data-state', status)
      badge.hidden = status === 'downloaded'
      badge.setAttribute('title', work.last_error || global.AccountCatalogue.labels[status] || '')
    })
    text(host.querySelector('[data-catalogue-footer]'), catalogueFooter(streamer))
    const loader = host.querySelector('.cv-loader')
    if (loader) loader.hidden = streamer.catalogue.scanState !== 'loading'
    const download = host.querySelector('[data-action="user-download"]')
    if (download) { download.disabled = !!global.state.busy.user; text(download, global.state.busy.user ? '下载处理中…' : '开始下载当前栏目') }
    const stop = host.querySelector('[data-action="account-preview-stop"]')
    if (stop) stop.disabled = streamer.catalogue.scanState !== 'loading'
    return true
  }

  function cardKind(work) {
    const kind = String(work && work.work_kind || '').toLowerCase()
    if (kind === 'article' || kind === 'gallery' || work.media_type === 'image') return 'gallery'
    return 'video'
  }

  function renderCard(work, index, secUid) {
    const source = String(work.cover_source || global.streamerMediaSource(work, 0) || '')
    const title = String(work.title || '无标题')
    const status = String(work.download_status || '')
    const pinned = work.is_pinned && !(work.catalogue && global.state.streamer.contentMode === 'like')
    const badges = []
    if (pinned) badges.push('<span class="cv-card-pin">置顶</span>')
    if (cardKind(work) === 'gallery') badges.push('<span class="cv-card-kind" title="图文">' + ICONS.gallery + '</span>')
    const overlays = []
    if (work.catalogue) {
      const label = global.AccountCatalogue.labels[status || 'unknown'] || '状态未知'
      overlays.push('<span class="cv-card-state" data-catalogue-status data-state="' + esc(status || 'unknown') + '" title="' +
        esc(work.last_error || label) + '"' + (status === 'downloaded' ? ' hidden' : '') + '>' + esc(label) + '</span>')
    }
    if (status === 'retryable_failed' || status === 'permanent_failed') {
      overlays.push('<span class="cv-card-error" title="' + esc(String(work.error_message || work.error_code || '未记录原因')) + '">' +
        esc(global.TaskPresentation.describe(status).label) + '</span>')
    }
    const offline = String(work.offline_label || '')
    if (offline) {
      overlays.push('<span class="cv-card-offline" title="' + esc(String(work.offline_reason || offline)) + '">' + ICONS.offline +
        '<span>' + esc(offline) + '</span></span>')
    }
    const cover = sharpCovers.get(source) || source
    const catalogueAttrs = work.catalogue
      ? ' data-catalogue-card="' + esc(work.aweme_id || '') + '" data-source-index="' + esc(work.source_index) + '"' : ''
    return '<li class="cv-card"' + catalogueAttrs + '>' +
      '<a href="#" class="cv-card-link" data-action="streamer-open-work" data-sec-uid="' + esc(secUid || '') + '" data-index="' + index +
        '" data-aweme-id="' + esc(work.aweme_id || '') + '" aria-label="' + esc('打开 ' + title) + '">' +
        '<div class="cv-card-thumb">' +
          (cover ? fadeImg('', cover, title, ' loading="lazy" decoding="async"') : '') +
          '<div class="cv-card-shade"></div>' +
          (badges.length ? '<div class="cv-card-badges">' + badges.join('') + '</div>' : '') +
          '<span class="cv-card-likes">' + ICONS.heart + '<span>' + esc(global.streamerCount(work.like_count)) + '</span></span>' +
          overlays.join('') +
        '</div>' +
        '<p class="cv-card-title">' + esc(title) + '</p>' +
      '</a>' +
    '</li>'
  }

  function renderHome() {
    const streamer = global.state.streamer
    const catalogue = streamer.catalogue
    const creator = streamer.creator || {}
    const collections = !!catalogue && streamer.homeSection === 'favorite_collection'
    const sourceName = catalogue ? '账号' : streamer.source === 'remote' ? '远端' : streamer.source === 'mixed' ? '远端和本地' : '本地'
    const works = collections ? [] : global.streamerFilteredWorks()
    const emptyText = collections ? '选择视频或合集，打开对应的下载条件' : streamer.loading ? '' : !catalogue && streamer.subtab === 'hidden' ? '暂无已隐藏作品' : streamer.search || streamer.date ? (catalogue ? '暂无符合条件的内容' : '暂无符合条件的作品') : '暂无' + sourceName + (catalogue ? '内容' : '作品')
    const firstLoad = !collections && !works.length && !streamer.search && !streamer.date &&
      (catalogue ? catalogue.scanState === 'loading' : streamer.loading)
    const cards = works.length
      ? works.map(work => renderCard(work, streamer.works.indexOf(work), streamer.secUid)).join('')
      : firstLoad ? '<li class="cv-skeleton-group"' + (catalogue ? ' data-catalogue-empty' : '') + '><ul class="cv-grid">' + skeletonCards(12) + '</ul><div class="cv-empty" hidden></div></li>'
      : '<li class="cv-empty-row"' + (catalogue ? ' data-catalogue-empty' : '') + '><div class="cv-empty" role="status">' + esc(emptyText) + '</div></li>'
    const tail = catalogue ? '<li hidden data-catalogue-tail data-request-id="' + esc(catalogue.requestId) + '"></li>' : ''
    const loading = !firstLoad && !collections && (catalogue ? catalogue.scanState === 'loading' : streamer.loading || streamer.loadingMore)
    const error = !catalogue && streamer.error ? '<div class="cv-notice">' + esc(streamer.error) + ' · 当前保留' + sourceName + '作品列表</div>' : ''
    const profileError = streamer.profileError ? '<div class="cv-notice">' + esc(streamer.profileError) + ' · 作品列表仍可查看</div>' : ''
    const actions = collections ? '' : catalogue
      ? '<button type="button" class="cv-button" data-action="account-preview-stop"' + (catalogue.scanState === 'loading' ? '' : ' disabled') + '>停止读取</button>' +
        '<button type="button" class="cv-button" data-action="account-home-refresh">刷新栏目</button>' +
        '<button type="button" class="button primary" data-action="user-download"' + (global.state.busy.user ? ' disabled' : '') + '>' + (global.state.busy.user ? '下载处理中…' : '开始下载当前栏目') + '</button>'
      : '<button type="button" class="cv-button" data-action="streamer-refresh"' + (streamer.loading || streamer.loadingMore ? ' disabled' : '') + '>' + (streamer.loading ? '刷新中…' : '刷新资料') + '</button>'
    const footer = collections ? '' : catalogue ? '<span data-catalogue-footer>' + esc(catalogueFooter(streamer)) + '</span>'
      : streamer.loading || streamer.loadingMore ? '正在加载…' : streamer.hasMore
        ? '<button type="button" class="cv-button" data-action="streamer-more">加载更多 / 重试</button>'
        : streamer.paginationStopped ? '分页加载已停止' : '已显示全部作品'
    return '<div class="cv-home' + (catalogue ? ' account-catalogue-home' : '') + '" data-creator-home>' +
      '<div class="cv-toolbar cv-toolbar-top"><button type="button" class="cv-button cv-back" data-action="streamer-back">' + ICONS.back +
        '<span>' + (streamer.returnPage === 'user' ? '返回用户作品' : '返回关注主播') + '</span></button><span class="cv-spacer"></span>' + actions + '</div>' +
      error + profileError +
      '<div class="cv-page">' + profileHtml(creator, streamer) + navigationHtml(creator, streamer) +
        '<ul class="cv-grid" id="cv-work-list" role="tabpanel">' + cards + tail + '</ul>' +
        '<div class="cv-loader" role="status" aria-label="正在加载"' + (loading ? '' : ' hidden') + '><span></span><span></span><span></span></div>' +
      '</div>' +
      '<div class="cv-toolbar cv-toolbar-bottom"><span class="cv-source" aria-live="polite">' + footer + '</span></div></div>'
  }

  // ── Player ─────────────────────────────────────────────────────────────

  function formatTime(seconds) {
    const value = Math.max(0, Math.floor(Number(seconds) || 0))
    const minutes = Math.floor(value / 60)
    return String(minutes).padStart(2, '0') + ':' + String(value % 60).padStart(2, '0')
  }

  function railHtml(work, creator) {
    const nickname = String(creator.nickname || '')
    const avatar = creator.avatar_url
      ? '<img src="' + esc(creator.avatar_url) + '" alt="' + esc(nickname) + '">'
      : '<span aria-hidden="true">' + esc(nickname.slice(0, 1) || '?') + '</span>'
    const item = (icon, label, count) =>
      '<button type="button" class="cv-rail-item" data-action="streamer-interaction" aria-label="' + label + '">' + icon +
      '<span>' + esc(global.streamerCount(count)) + '</span></button>'
    // Like / collect are shown locally only (the app cannot send them to Douyin).
    const reaction = global.streamerReaction ? global.streamerReaction(work) : {}
    const toggle = (kind, icon, label, count) =>
      '<button type="button" class="cv-rail-item cv-rail-' + kind + (reaction[kind] ? ' is-on' : '') + '" data-action="streamer-' + kind +
      '" aria-pressed="' + !!reaction[kind] + '" aria-label="' + label + '"><span class="cv-rail-icon">' + icon + '</span>' +
      '<span>' + esc(global.streamerCount(count)) + '</span></button>'
    const commentsOpen = !!global.state.streamer.commentsOpen
    const comments = '<button type="button" class="cv-rail-item' + (commentsOpen ? ' is-active' : '') +
      '" data-action="streamer-comments-toggle" aria-pressed="' + commentsOpen + '" aria-label="评论（X）">' + ICONS.comment +
      '<span>' + esc(global.streamerCount(work.comment_count)) + '</span></button>'
    return '<aside class="cv-rail">' +
      '<span class="cv-rail-avatar">' + avatar + '</span>' +
      toggle('like', ICONS.heartFill, '点赞', work.like_count) + comments +
      toggle('collect', ICONS.star, '收藏', work.collect_count) + item(ICONS.share, '分享', work.share_count) +
      // The former 看相关 slot stays empty so nothing above it moves.
      '<span class="cv-rail-item cv-rail-placeholder" aria-hidden="true">' + ICONS.share + '<span>0</span></span>' +
    '</aside>'
  }

  function captionHtml(work, creator, isImage, modal) {
    const publishTime = global.streamerPublishTime(work.create_time)
    const notice = modal.error
      ? '<div class="cv-notice cv-media-notice" role="alert">' + esc(modal.error || '媒体暂时无法加载') +
        ' <button type="button" data-action="streamer-retry">重新加载</button></div>' : ''
    return '<div class="cv-caption">' + notice +
      '<div class="cv-author"><strong>@' + esc(creator.nickname || '未知主播') + '</strong>' +
        (publishTime ? '<span class="cv-publish-time">· ' + esc(publishTime) + '</span>' : '') +
        (isImage ? '<span class="cv-kind-tag">' + ICONS.gallery + '图文</span>' : '') + '</div>' +
      '<p class="cv-desc">' + esc(work.title || '无标题') + '</p>' +
    '</div>'
  }

  function controlsHtml(isImage) {
    const toggle = (action, label, key) =>
      '<button type="button" class="cv-toggle" data-action="' + action + '" title="' + label + '（' + key + '）"><span class="cv-switch"></span>' + label + '</button>'
    return '<div class="cv-controls">' +
      '<button type="button" class="cv-control cv-play" data-action="streamer-video-play" aria-label="播放或暂停">' +
        '<span class="cv-icon-play">' + ICONS.play + '</span><span class="cv-icon-pause">' + ICONS.pause + '</span></button>' +
      (isImage ? '' : '<span class="cv-time"><span data-cv-current>00:00</span> / <span data-cv-duration>00:00</span></span>') +
      '<span class="cv-spacer"></span>' +
      toggle('streamer-auto-play', '连播', 'K') + toggle('streamer-clear-screen', '清屏', 'J') +
      '<div class="cv-volume">' +
        '<button type="button" class="cv-control" data-action="streamer-video-volume" aria-label="静音或取消静音">' +
          '<span class="cv-icon-volume">' + ICONS.volume + '</span><span class="cv-icon-muted">' + ICONS.muted + '</span></button>' +
        '<div class="cv-volume-panel" data-streamer-volume-slider>' +
          '<span class="cv-volume-label">100</span>' +
          '<div class="cv-volume-bar" data-streamer-volume-bar><div class="cv-volume-fill" style="height:100%"></div></div>' +
        '</div>' +
      '</div>' +
      '<button type="button" class="cv-control" data-action="streamer-video-fullscreen" aria-label="全屏（H）">' + ICONS.fullscreen + '</button>' +
    '</div>'
  }

  function stageHtml(work, modal, isImage) {
    if (!isImage) {
      const source = global.streamerMediaSource(work, modal.mediaIndex)
      const poster = work.cover_source ? ' poster="' + esc(work.cover_source) + '"' : ''
      return '<div class="cv-stage">' +
        '<video id="streamer-media-video" class="cv-video" data-aweme-id="' + esc(work.aweme_id || '') + '"' +
          (source ? ' src="' + esc(source) + '"' : '') + poster + (source && !modal.loading && global.AppPreferences.mediaAutoplay() ? ' autoplay' : '') +
          ' playsinline webkit-playsinline x5-playsinline' + (modal.muted ? ' muted' : '') + '></video>' +
        '<button type="button" class="cv-start" data-action="streamer-video-play" aria-label="播放">' + ICONS.bigPlay + '</button>' +
      '</div>'
    }
    const sources = Array.isArray(work.media_sources) ? work.media_sources : []
    const index = Math.min(Math.max(0, Number(modal.mediaIndex || 0)), Math.max(0, sources.length - 1))
    const current = sources[index] || ''
    const music = global.streamerMusicSources(work)
    return '<div class="cv-stage">' +
      '<div class="cv-backdrop"><img alt=""' + (current ? ' src="' + esc(current) + '"' : '') + '></div>' +
      '<img class="cv-picture" alt="' + esc(work.title || '') + '"' + (current ? ' src="' + esc(current) + '"' : '') + '>' +
      '<button type="button" class="cv-arrow cv-arrow-prev" data-action="streamer-media-prev" aria-label="上一张"' + (index <= 0 ? ' disabled' : '') + '>' + ICONS.left + '</button>' +
      '<button type="button" class="cv-arrow cv-arrow-next" data-action="streamer-media-next" aria-label="下一张"' + (index >= sources.length - 1 ? ' disabled' : '') + '>' + ICONS.right + '</button>' +
      // The soundtrack loops on its own; the pictures run on their own clock.
      '<audio id="streamer-media-audio" class="streamer-modal-audio" loop' + (music[0] ? ' src="' + esc(music[0]) + '"' : '') + '>' +
        music.map(src => '<source src="' + esc(src) + '">').join('') + '</audio>' +
    '</div>'
  }

  function rebuildPictureSegments(root, total, current, progress) {
    const list = root.querySelector('.cv-progress-segments')
    if (!list || !total) return
    list.innerHTML = Array.from({ length: total }, (_, index) => {
      const width = index < current ? 100 : (index === current ? progress : 0)
      return '<span class="cv-progress-track" data-streamer-segment="' + index + '"><span class="cv-progress-played" style="width:' + width + '%"></span></span>'
    }).join('')
  }

  // A live photo picture plays its muted clip in place of the still; the
  // gallery soundtrack keeps coming from the music player.
  function liveSource(work, index) {
    const sources = Array.isArray(work && work.live_sources) ? work.live_sources : []
    return String(sources[index] || '')
  }
  // A play() right after the clip is inserted or paused can be aborted by
  // its load; retry once the clip is ready unless the gallery was paused.
  function playLiveClip(clip) {
    if (!global.state?.streamer?.modal?.mediaStarted) return
    if (!clip.paused || !clip.isConnected) return
    const result = clip.play()
    if (!result || typeof result.catch !== 'function') return
    result.catch(error => {
      if (!error || error.name !== 'AbortError') return
      clip.addEventListener('canplay', () => {
        const modal = global.state && global.state.streamer && global.state.streamer.modal
        if (!modal || !modal.galleryPaused) playLiveClip(clip)
      }, { once: true })
      if (clip.readyState >= 3) setTimeout(() => clip.dispatchEvent(new Event('canplay')), 0)
    })
  }
  function syncLiveClip(root, work, index, playing) {
    const live = liveSource(work, index)
    root.querySelectorAll('img.cv-picture').forEach(image => {
      let clip = image.nextElementSibling
      if (!clip || !clip.classList.contains('streamer-live-clip')) clip = null
      if (!live) {
        if (clip) clip.remove()
        image.style.removeProperty('display')
        return
      }
      if (!clip) {
        clip = document.createElement('video')
        clip.className = 'cv-picture streamer-live-clip'
        for (const name of ['muted', 'loop', 'playsinline']) clip.setAttribute(name, '')
        clip.setAttribute('preload', 'auto')
        clip.setAttribute('aria-hidden', 'true')
        image.insertAdjacentElement('afterend', clip)
      }
      clip.muted = true
      if (clip.getAttribute('src') !== live) clip.setAttribute('src', live)
      image.style.display = 'none'
      if (playing === false) clip.pause()
      else if (playing === true) playLiveClip(clip)
    })
  }

  function renderModal() {
    const modal = global.state.streamer.modal
    const work = global.streamerCurrentWork()
    if (!modal || !work) return ''
    const isImage = work.media_type === 'image'
    const creator = (work.catalogue ? work.author_profile : global.state.streamer.creator) || {}
    const sources = Array.isArray(work.media_sources) ? work.media_sources : []
    const progress = isImage
      ? '<div class="cv-progress cv-progress-pictures" data-streamer-progress-bar data-streamer-picture-progress><div class="cv-progress-segments"></div></div>'
      : '<div class="cv-progress" data-streamer-progress-bar><span class="cv-progress-track"><span class="cv-progress-played" style="width:' + (Number(modal.progress || 0) / 10) + '%"></span></span></div>'
    const root = document.createElement('div')
    const commentsOpen = !!global.state.streamer.commentsOpen && !!global.streamerCommentsPanel
    root.innerHTML = '<div id="streamer-modal" class="cv-player ' + (isImage ? 'cv-player-images' : 'cv-player-video') + (commentsOpen ? ' has-comments' : '') +
      '" role="dialog" aria-modal="true" aria-label="作品详情" tabindex="-1">' +
      stageHtml(work, modal, isImage) +
      '<button type="button" class="cv-close" data-action="streamer-modal-close" aria-label="关闭">' + ICONS.close + '</button>' +
      '<div class="cv-work-switch">' +
        '<button type="button" data-action="streamer-work-prev" aria-label="上一个作品">' + ICONS.up + '</button>' +
        '<button type="button" data-action="streamer-work-next" aria-label="下一个作品">' + ICONS.down + '</button>' +
      '</div>' +
      railHtml(work, creator) +
      '<div class="cv-bottom">' + captionHtml(work, creator, isImage, modal) + progress + controlsHtml(isImage) + '</div>' +
      (modal.relatedOpen && global.streamerRelatedPanel ? global.streamerRelatedPanel() : '') +
      (commentsOpen ? global.streamerCommentsPanel() : '') +
    '</div>'
    const player = root.firstElementChild
    if (isImage) {
      const index = Math.min(Math.max(0, Number(modal.mediaIndex || 0)), Math.max(0, sources.length - 1))
      syncLiveClip(player, work, index)
      rebuildPictureSegments(player, sources.length, index, Number(modal.pictureProgress || 0))
    }
    return player.outerHTML
  }

  function mountMediaNotice(root, notice) {
    notice.className = 'cv-notice cv-media-notice'
    notice.setAttribute('role', 'alert')
    const caption = root.querySelector('.cv-caption')
    if (caption) caption.insertBefore(notice, caption.firstChild)
    else root.appendChild(notice)
  }

  function bindPlayerChrome(root) {
    if (!root) return
    if (root.classList.contains('cv-player-images')) {
      const showImageFailure = image => {
        const session = global.state && global.state.streamer
        const modal = session && session.modal
        const work = modal && session.works[Number(modal.workIndex)]
        const index = Number(modal && modal.mediaIndex || 0)
        const source = global.streamerMediaSource && global.streamerMediaSource(work, index)
        if (!root.isConnected || !modal || modal.loading || !work || !source || image.getAttribute('src') !== source) return
        if (liveSource(work, index)) return

        image.style.visibility = 'hidden'
        let notice = root.querySelector('.cv-media-notice')
        if (!notice) {
          notice = document.createElement('div')
          mountMediaNotice(root, notice)
        }
        const message = '第 ' + (index + 1) + ' 张图片加载失败，可重新获取该作品的图片地址。'
        const textNode = Array.from(notice.childNodes).find(node => node.nodeType === Node.TEXT_NODE)
        if (textNode) textNode.nodeValue = message + ' '
        else notice.insertBefore(document.createTextNode(message + ' '), notice.firstChild)
        let retry = notice.querySelector('[data-action="streamer-retry"]')
        if (!retry) {
          retry = document.createElement('button')
          retry.type = 'button'
          retry.setAttribute('data-action', 'streamer-retry')
          notice.appendChild(retry)
        }
        retry.textContent = '重新获取图片地址'
      }
      root.querySelectorAll('img.cv-picture').forEach(image => {
        image.addEventListener('load', () => { image.style.visibility = '' })
        image.addEventListener('error', () => showImageFailure(image))
        if (image.complete && image.naturalWidth === 0 && image.getAttribute('src')) showImageFailure(image)
      })
      return
    }
    // The preloaded <video> may have replaced the rendered one before binding.
    const video = root.querySelector('#streamer-media-video')
    const current = root.querySelector('[data-cv-current]')
    const duration = root.querySelector('[data-cv-duration]')
    if (!video) return
    const update = () => {
      text(current, formatTime(video.currentTime))
      if (Number.isFinite(video.duration)) text(duration, formatTime(video.duration))
    }
    video.addEventListener('timeupdate', update)
    video.addEventListener('loadedmetadata', update)
    video.addEventListener('durationchange', update)
    update()
  }

  // The shell already resizes columns continuously. Only soften the discrete
  // size step when auto-fill adds/removes a column; cards take their new row
  // immediately, without flying across the grid. IntersectionObserver keeps
  // both geometry reads and animations limited to the visible viewport.
  const gridMotionPreference = global.matchMedia('(prefers-reduced-motion: reduce)')
  let gridMotion = null
  function bindGridMotion(grid) {
    if (gridMotion && gridMotion.grid === grid) return
    if (gridMotion) gridMotion.dispose()
    gridMotion = null
    if (!grid || typeof global.ResizeObserver !== 'function' ||
      typeof global.IntersectionObserver !== 'function' || typeof grid.animate !== 'function') return
    const visible = new Set(), observed = new Set(), animations = new Map()
    const cancel = card => { animations.get(card)?.cancel(); animations.delete(card) }
    const cancelAll = () => { animations.forEach(animation => animation.cancel()); animations.clear() }
    const viewportTop = (document.querySelector('.cv-toolbar-top')?.getBoundingClientRect().height || 0) +
      (document.querySelector('.titlebar')?.offsetHeight || 0)
    const viewportBottom = document.querySelector('.status')?.getBoundingClientRect().height || 0
    const visibility = new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (entry.isIntersecting && entry.intersectionRect.width > 0 && entry.intersectionRect.height > 0) visible.add(entry.target)
        else { visible.delete(entry.target); cancel(entry.target) }
      }
    }, { rootMargin: `-${viewportTop}px 0px -${viewportBottom}px 0px` })
    const cardsIn = node => node.nodeType !== 1 ? [] : node.matches('.cv-card') ? [node] : node.querySelectorAll('.cv-card')
    const observe = card => { observed.add(card); visibility.observe(card) }
    grid.querySelectorAll('.cv-card').forEach(observe)
    const children = new MutationObserver(changes => {
      for (const change of changes) {
        for (const node of change.removedNodes) for (const card of cardsIn(node)) {
          visibility.unobserve(card); observed.delete(card); visible.delete(card); cancel(card)
        }
        for (const node of change.addedNodes) for (const card of cardsIn(node)) {
          if (!observed.has(card)) observe(card)
        }
      }
    })
    const tracks = () => getComputedStyle(grid).gridTemplateColumns.split(/\s+/).filter(Boolean).map(parseFloat)
    let columns = tracks()
    const resize = new ResizeObserver(() => {
      const next = tracks(), previous = columns
      columns = next
      // No per-card reads or animation restarts during continuous shell resize.
      if (next.length === previous.length || !global.AppPreferences.motionAllowed() || !(next[0] > 0) || !(previous[0] > 0)) return
      const measured = []
      for (const card of visible) {
        if (!card.isConnected) continue
        const rect = card.getBoundingClientRect()
        if (rect.bottom <= viewportTop || rect.top >= global.innerHeight - viewportBottom || rect.right <= 0 || rect.left >= global.innerWidth) continue
        const transform = getComputedStyle(card).transform
        const scale = transform === 'none' ? 1 : new DOMMatrixReadOnly(transform).a
        measured.push({ card, scale: previous[0] * scale / next[0] })
      }
      for (const { card, scale } of measured) {
        cancel(card)
        if (Math.abs(scale - 1) < .001) continue
        // Uniform scale keeps covers undistorted. The image itself retains its
        // independent loading fade / hover zoom. No translate is used here.
        const animation = card.animate([{ transform: `scale(${scale})` }, { transform: 'none' }],
          { duration: 180, easing: 'cubic-bezier(.2, 0, 0, 1)' })
        animation.startTime = document.timeline.currentTime
        animations.set(card, animation)
        animation.onfinish = () => { if (animations.get(card) === animation) animations.delete(card) }
      }
    })
    const reduceMotion = () => { if (!global.AppPreferences.motionAllowed()) cancelAll() }
    resize.observe(grid)
    children.observe(grid, { childList: true, subtree: true })
    gridMotionPreference.addEventListener('change', reduceMotion)
    global.addEventListener('app-preferences-changed', reduceMotion)
    gridMotion = { grid, dispose() {
      resize.disconnect(); visibility.disconnect(); children.disconnect()
      gridMotionPreference.removeEventListener('change', reduceMotion)
      global.removeEventListener('app-preferences-changed', reduceMotion)
      cancelAll(); visible.clear(); observed.clear()
    } }
  }

  function applyPageChrome() {
    const on = global.state.page === 'streamer'
    document.documentElement.classList.toggle('creator-page', on)
    const content = document.getElementById('content')
    if (content) content.classList.toggle('creator-content', on)
    bindGridMotion(on ? document.getElementById('cv-work-list') : null)
  }

  // Downloaded covers are full-size originals squeezed into ~250px cards.
  // Chromium's static raster downsamples them with mipmap+bilinear, which
  // turns hair and fur into grain; pre-shrink them with a high-quality
  // resampler to the card's device-pixel size instead.
  const sharpCovers = new Map()
  const SHARP_COVER_LIMIT = 400
  function sharpCoverTarget(image) {
    const box = image.parentElement
    if (!box || !image.naturalWidth || !image.naturalHeight) return null
    // 1.1 leaves headroom for the hover zoom.
    const ratio = (global.devicePixelRatio || 1) * 1.1
    const scale = Math.max(box.clientWidth / image.naturalWidth, box.clientHeight / image.naturalHeight) * ratio
    if (!(scale > 0) || scale > 0.6) return null
    return { width: Math.round(image.naturalWidth * scale), height: Math.round(image.naturalHeight * scale) }
  }
  async function sharpenCover(image) {
    const source = image.getAttribute('src') || ''
    // Only local covers: online CDN covers are already card-sized and refuse CORS.
    if (!/^(data:|http:\/\/127\.0\.0\.1:)/.test(source) || image.dataset.sharpCover === source) return
    image.dataset.sharpCover = source
    let url = sharpCovers.get(source)
    if (!url) {
      const target = sharpCoverTarget(image)
      if (!target || typeof global.createImageBitmap !== 'function') return
      try {
        // Decode from fetched bytes: local covers come from the loopback media
        // server (another origin), and an <img> from there would taint the canvas.
        const response = await fetch(source)
        if (!response.ok) return
        const bitmap = await global.createImageBitmap(await response.blob(), {
          resizeWidth: target.width, resizeHeight: target.height, resizeQuality: 'high',
        })
        const canvas = document.createElement('canvas')
        canvas.width = bitmap.width
        canvas.height = bitmap.height
        canvas.getContext('2d').drawImage(bitmap, 0, 0)
        bitmap.close()
        const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.92))
        if (!blob) return
        url = URL.createObjectURL(blob)
      } catch (error) {
        console.warn('cover resample failed', error)
        return
      }
      sharpCovers.set(source, url)
      if (sharpCovers.size > SHARP_COVER_LIMIT) {
        const [oldest, oldUrl] = sharpCovers.entries().next().value
        sharpCovers.delete(oldest)
        if (!document.querySelector('img[src="' + oldUrl + '"]')) URL.revokeObjectURL(oldUrl)
      }
    }
    if (image.isConnected && image.getAttribute('src') === source) image.setAttribute('src', url)
  }
  document.addEventListener('load', event => {
    const image = event.target
    if (image instanceof HTMLImageElement && image.closest('.cv-home .cv-card-thumb')) sharpenCover(image)
  }, true)

  global.CreatorView = {
    renderHome,
    syncCatalogue,
    renderModal,
    bindPlayerChrome,
    applyPageChrome,
    rebuildPictureSegments,
    syncLiveClip,
    playLiveClip,
  }
})(window)
