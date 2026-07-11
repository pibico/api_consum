/**
 * landing.js — screenshot carousel (vanilla, no deps).
 * Arrows + dots + 6s auto-advance (paused on hover / hidden tab).
 */
(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    var track = document.getElementById('car-track');
    var dotsBox = document.getElementById('car-dots');
    if (!track) return;
    var slides = track.children.length;
    var idx = 0;
    var timer = null;

    for (var i = 0; i < slides; i++) {
      var b = document.createElement('button');
      b.className = 'ld-car-dot';
      b.setAttribute('aria-label', 'Slide ' + (i + 1));
      (function (n) { b.onclick = function () { go(n); }; })(i);
      dotsBox.appendChild(b);
    }
    var dots = dotsBox.children;

    function go(n) {
      idx = (n + slides) % slides;
      track.style.transform = 'translateX(-' + (idx * 100) + '%)';
      for (var i = 0; i < slides; i++) {
        dots[i].classList.toggle('active', i === idx);
      }
    }

    function arm() {
      clearInterval(timer);
      timer = setInterval(function () { go(idx + 1); }, 6000);
    }

    document.getElementById('car-prev').onclick = function () { go(idx - 1); arm(); };
    document.getElementById('car-next').onclick = function () { go(idx + 1); arm(); };
    var box = document.getElementById('ld-carousel');
    box.addEventListener('mouseenter', function () { clearInterval(timer); });
    box.addEventListener('mouseleave', arm);
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) clearInterval(timer); else arm();
    });

    // Swipe (touch)
    var x0 = null;
    box.addEventListener('touchstart', function (e) { x0 = e.touches[0].clientX; }, { passive: true });
    box.addEventListener('touchend', function (e) {
      if (x0 == null) return;
      var dx = e.changedTouches[0].clientX - x0;
      if (Math.abs(dx) > 40) { go(idx + (dx < 0 ? 1 : -1)); arm(); }
      x0 = null;
    }, { passive: true });

    go(0);
    arm();
  });
})();
