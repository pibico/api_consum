/* ============================================
   pibiCo Tabs — SVG mask tab switcher
   Concave Q-bezier scoops, desktop + mobile
   ============================================ */

var R  = 14;   // concave scoop radius
var cR = 10;   // tab top corner radius
var bR = 20;   // card bottom corner radius

function isMobile() {
    return false; // MemorIA always uses horizontal tabs
}

function applyMask(el, W, H, d) {
    var key = W + 'x' + H + '|' + d;
    if (el._maskKey === key) return;
    el._maskKey = key;
    var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="' + W + '" height="' + H + '">' +
              '<path d="' + d + '" fill="white"/></svg>';
    var uri = 'url("data:image/svg+xml,' + encodeURIComponent(svg) + '")';
    el.style.maskImage = uri;
    el.style.webkitMaskImage = uri;
    el.style.maskSize = W + 'px ' + H + 'px';
    el.style.webkitMaskSize = W + 'px ' + H + 'px';
    el.style.maskRepeat = 'no-repeat';
    el.style.webkitMaskRepeat = 'no-repeat';
}

function _activeInfo(row, container) {
    var a = row.querySelector('.pibico-tab.active');
    if (!a) return null;
    var tabs = row.querySelectorAll('.pibico-tab');
    var aTop = a.offsetTop;
    var rowTabs = [];
    tabs.forEach(function(t) { if (t.offsetTop === aTop) rowTabs.push(t); });
    /* Sort by visual position (offsetLeft) — flex order:1 etc. can decouple
       DOM order from visual order, breaking first/last-of-row detection. */
    rowTabs.sort(function(x, y) { return x.offsetLeft - y.offsetLeft; });
    var idxInRow = rowTabs.indexOf(a);
    var prev = idxInRow > 0 ? rowTabs[idxInRow - 1] : null;
    var next = idxInRow < rowTabs.length - 1 ? rowTabs[idxInRow + 1] : null;
    var tT = a.offsetTop + row.offsetTop;
    var tL = a.offsetLeft + row.offsetLeft;
    var tW = a.offsetWidth;
    var tH = a.offsetHeight;
    var tR = tL + tW;
    var leftGap = prev ? (tL - (prev.offsetLeft + row.offsetLeft + prev.offsetWidth)) : Infinity;
    var rightGap = next ? ((next.offsetLeft + row.offsetLeft) - tR) : Infinity;
    var leftBound = prev ? Math.min(leftGap + prev.offsetWidth / 2, R) : R;
    var rightBound = next ? Math.min(rightGap + next.offsetWidth / 2, R) : R;
    return {
        tT: tT, tB: tT + tH, tL: tL, tR: tR, tW: tW,
        Rl: Math.max(2, Math.min(R, leftBound)),
        Rr: Math.max(2, Math.min(R, rightBound)),
        cRa: Math.max(2, Math.min(cR, Math.floor(tW / 2) - 1)),
        isFirstOfRow: !prev,
        isLastOfRow:  !next,
    };
}

function buildHorizontalMask(glassEl, activeTab, container) {
    var W = container.offsetWidth;
    var H = container.offsetHeight;

    var rows = Array.from(container.querySelectorAll('.pibico-tab-row'));
    if (!rows.length) return;

    /* Collect active tab info per row, sorted top-to-bottom */
    var rowInfos = rows.map(function(row) {
        var info = _activeInfo(row, container);
        return info ? { info: info, row: row } : null;
    }).filter(Boolean);
    if (!rowInfos.length) return;
    rowInfos.sort(function(a, b) { return a.info.tT - b.info.tT; });

    /* Bottom-most row scoops INTO card body. Upper rows get standalone tab fingers. */
    var bottom = rowInfos[rowInfos.length - 1].info;
    var rowH = bottom.tB;

    var d = 'M0,' + rowH;
    /* Always draw both concave scoops at active tab edges — even when active is
       first or last in its row (scoop curls from card baseline UP into tab). */
    if (bottom.tL > 0) {
        d += ' L' + (bottom.tL - bottom.Rl) + ',' + rowH;
        d += ' Q' + bottom.tL + ',' + rowH + ' ' + bottom.tL + ',' + (rowH - bottom.Rl);
        d += ' L' + bottom.tL + ',' + (bottom.tT + bottom.cRa);
        d += ' Q' + bottom.tL + ',' + bottom.tT + ' ' + (bottom.tL + bottom.cRa) + ',' + bottom.tT;
    } else {
        d += ' L0,' + (bottom.tT + bottom.cRa);
        d += ' Q0,' + bottom.tT + ' ' + bottom.cRa + ',' + bottom.tT;
    }
    if (bottom.tR < W) {
        d += ' L' + (bottom.tR - bottom.cRa) + ',' + bottom.tT;
        d += ' Q' + bottom.tR + ',' + bottom.tT + ' ' + bottom.tR + ',' + (bottom.tT + bottom.cRa);
        d += ' L' + bottom.tR + ',' + (rowH - bottom.Rr);
        d += ' Q' + bottom.tR + ',' + rowH + ' ' + (bottom.tR + bottom.Rr) + ',' + rowH;
    } else {
        d += ' L' + (bottom.tR - bottom.cRa) + ',' + bottom.tT;
        d += ' Q' + bottom.tR + ',' + bottom.tT + ' ' + bottom.tR + ',' + (bottom.tT + bottom.cRa);
        d += ' L' + bottom.tR + ',' + rowH;
    }
    /* Continue along card body to right edge + bottom corners + close */
    d += ' L' + W + ',' + rowH;
    d += ' L' + W + ',' + (H - bR);
    d += ' Q' + W + ',' + H + ' ' + (W - bR) + ',' + H;
    d += ' L' + bR + ',' + H;
    d += ' Q0,' + H + ' 0,' + (H - bR);
    d += ' Z';

    /* Upper-row tab fingers as separate subpaths (rounded top, concave bottom) */
    for (var i = 0; i < rowInfos.length - 1; i++) {
        var t = rowInfos[i].info;
        if (t.tL > 0) {
            d += ' M' + (t.tL - t.Rl) + ',' + t.tB;
            d += ' Q' + t.tL + ',' + t.tB + ' ' + t.tL + ',' + (t.tB - t.Rl);
            d += ' L' + t.tL + ',' + (t.tT + t.cRa);
            d += ' Q' + t.tL + ',' + t.tT + ' ' + (t.tL + t.cRa) + ',' + t.tT;
        } else {
            d += ' M0,' + t.tB;
            d += ' L0,' + (t.tT + t.cRa);
            d += ' Q0,' + t.tT + ' ' + t.cRa + ',' + t.tT;
        }
        if (t.tR < W) {
            d += ' L' + (t.tR - t.cRa) + ',' + t.tT;
            d += ' Q' + t.tR + ',' + t.tT + ' ' + t.tR + ',' + (t.tT + t.cRa);
            d += ' L' + t.tR + ',' + (t.tB - t.Rr);
            d += ' Q' + t.tR + ',' + t.tB + ' ' + (t.tR + t.Rr) + ',' + t.tB;
        } else {
            d += ' L' + (t.tR - t.cRa) + ',' + t.tT;
            d += ' Q' + t.tR + ',' + t.tT + ' ' + t.tR + ',' + (t.tT + t.cRa);
            d += ' L' + t.tR + ',' + t.tB;
        }
        d += ' Z';
    }

    applyMask(glassEl, W, H, d);
}

function buildVerticalMask(glassEl, activeTab, container) {
    var W = container.offsetWidth;
    var H = container.offsetHeight;
    var tabs = container.querySelectorAll('.pibico-tab');
    var tabRow = container.querySelector('.pibico-tab-row');
    if (!tabRow || !activeTab) return;

    var Rv = 12;   // vertical scoop radius
    var cRv = 8;   // vertical tab corner radius
    var bRv = 16;  // vertical card body corner radius

    var colW = tabRow.offsetWidth;
    var tT = activeTab.offsetTop;
    var tH = activeTab.offsetHeight;
    var tB = tT + tH;
    var isFirst = (activeTab === tabs[0]);
    var isLast  = (activeTab === tabs[tabs.length - 1]);

    // Start: top-left of content area (right of tab column)
    var d = 'M' + colW + ',0';

    // Move down to where active tab starts
    // Top scoop
    if (isFirst) {
        d += ' L' + cRv + ',0';
        d += ' Q0,0 0,' + cRv;
        d += ' L0,' + tT;
    } else {
        d += ' L' + colW + ',' + (tT - Rv);
        d += ' Q' + colW + ',' + tT + ' ' + (colW - Rv) + ',' + tT;
        d += ' L' + cRv + ',' + tT;
        d += ' Q0,' + tT + ' 0,' + (tT + cRv);
    }

    // Down active tab
    if (isLast) {
        d += ' L0,' + (tB - cRv);
        d += ' Q0,' + tB + ' 0,' + tB;
        d += ' L0,' + H;
    } else {
        d += ' L0,' + (tB - cRv);
        d += ' Q0,' + tB + ' ' + cRv + ',' + tB;
        d += ' L' + (colW - Rv) + ',' + tB;
        d += ' Q' + colW + ',' + tB + ' ' + colW + ',' + (tB + Rv);
    }

    // Continue down to bottom-left of content area
    if (isLast) {
        d += ' L' + colW + ',' + H;
    } else {
        d += ' L' + colW + ',' + H;
    }

    // Bottom-right corner
    d += ' L' + (W - bRv) + ',' + H;
    d += ' Q' + W + ',' + H + ' ' + W + ',' + (H - bRv);

    // Up right side
    d += ' L' + W + ',' + bRv;
    d += ' Q' + W + ',0 ' + (W - bRv) + ',0';

    d += ' Z';

    applyMask(glassEl, W, H, d);
}

function switchTab(tabEl) {
    var cardArea = tabEl.closest('.pibico-card-area');
    if (!cardArea) return;

    /* Deactivate sibling tabs WITHIN THE SAME ROW only — multi-row layouts
       (e.g. notes: module nav + secondary classification) keep one active per row. */
    var ownRow = tabEl.closest('.pibico-tab-row') || cardArea;
    var tabs = ownRow.querySelectorAll('.pibico-tab');
    tabs.forEach(function(t) { t.classList.remove('active'); });

    // Activate clicked tab
    tabEl.classList.add('active');

    // Show matching content section, hide others
    var targetId = tabEl.getAttribute('data-tab');
    if (targetId) {
        var sections = cardArea.querySelectorAll('.pibico-tab-content');
        sections.forEach(function(s) { s.style.display = 'none'; });
        var target = cardArea.querySelector('#' + targetId);
        if (target) target.style.display = '';
    }

    // Update section title if present
    var title = cardArea.querySelector('.pibico-section-title');
    var tabTitle = tabEl.getAttribute('data-title');
    if (title && tabTitle) {
        title.textContent = tabTitle;
    }

    // Mark active-alone (drives CSS flex-grow only when active is solo on its row)
    _markActiveAloneOnRow(cardArea);

    // Rebuild mask — twice if active-alone class changed widths (rAF for reflow)
    var glassEl = cardArea.querySelector('.pibico-glass');
    if (glassEl) {
        if (isMobile()) {
            buildVerticalMask(glassEl, tabEl, cardArea);
        } else {
            buildHorizontalMask(glassEl, tabEl, cardArea);
            requestAnimationFrame(function() {
                buildHorizontalMask(glassEl, tabEl, cardArea);
            });
        }
    }
}

function initTabs() {
    var cardAreas = document.querySelectorAll('.pibico-card-area');
    cardAreas.forEach(function(area) {
        var activeTab = area.querySelector('.pibico-tab.active');
        if (!activeTab) {
            activeTab = area.querySelector('.pibico-tab');
            if (activeTab) activeTab.classList.add('active');
        }
        if (activeTab) {
            switchTab(activeTab);
        }
    });
}

function _markActiveAloneOnRow(area) {
    /* For each .pibico-tab-row in area, flag the active tab with .active-alone
       when it's the only tab on its visual row. Used by CSS to flex-grow only
       when alone (avoids stretching when siblings share the row). */
    var rows = area.querySelectorAll('.pibico-tab-row');
    rows.forEach(function(row) {
        var tabs = row.querySelectorAll('.pibico-tab');
        var active = row.querySelector('.pibico-tab.active');
        tabs.forEach(function(t) { t.classList.remove('active-alone'); });
        if (!active) return;
        var aTop = active.offsetTop;
        var sameRowCount = 0;
        tabs.forEach(function(t) { if (t.offsetTop === aTop) sameRowCount++; });
        if (sameRowCount === 1) active.classList.add('active-alone');
    });
}

function rebuildAllMasks() {
    var cardAreas = document.querySelectorAll('.pibico-card-area');
    cardAreas.forEach(function(area) {
        var activeTab = area.querySelector('.pibico-tab.active');
        if (activeTab) {
            _markActiveAloneOnRow(area);
            var glassEl = area.querySelector('.pibico-glass');
            if (glassEl) {
                if (isMobile()) {
                    buildVerticalMask(glassEl, activeTab, area);
                } else {
                    /* Re-evaluate after flex-grow class change — width may shift. */
                    buildHorizontalMask(glassEl, activeTab, area);
                    requestAnimationFrame(function() {
                        buildHorizontalMask(glassEl, activeTab, area);
                    });
                }
            }
        }
    });
}

window.pibicoTabsRebuild = rebuildAllMasks;

document.addEventListener('DOMContentLoaded', function() {
    initTabs();
    /* Watch each card area for size changes (splitters, panels, devtools resize)
       — re-mark active-alone + rebuild SVG mask when card width crosses wrap points. */
    if (typeof ResizeObserver !== 'undefined') {
        var _roTimer;
        var ro = new ResizeObserver(function() {
            clearTimeout(_roTimer);
            _roTimer = setTimeout(rebuildAllMasks, 60);
        });
        document.querySelectorAll('.pibico-card-area').forEach(function(area) {
            ro.observe(area);
        });
    }
});

var _resizeTimer;
window.addEventListener('resize', function() {
    clearTimeout(_resizeTimer);
    _resizeTimer = setTimeout(rebuildAllMasks, 100);
});
