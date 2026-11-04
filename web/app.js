let currentAnalysis = null;
let activeSymbol = "BTC";

function fmtPx(px) {
  const n = Number(px || 0);
  if (Math.abs(n) >= 1000) return n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (Math.abs(n) >= 1) return n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 4 });
  return n.toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
}

function showToast(msg) {
  const el = document.getElementById("toastBox");
  el.textContent = msg;
  el.style.display = "block";
  clearTimeout(window._toastTimer);
  window._toastTimer = setTimeout(() => {
    el.style.display = "none";
  }, 4200);
}

// Tab switching
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    const tab = btn.dataset.tab;
    document.getElementById(`tab-${tab}`).classList.add("active");

    if (tab === "copytrader") loadCopyTraders();
    if (tab === "telegram") {
      loadTelegramLogs();
      loadSignalsArchive();
    }
    if (tab === "settings") loadSettings();
    if (tab === "terminal" && currentAnalysis) {
      setTimeout(() => drawCandlestickChart(currentAnalysis), 60);
    }
  });
});

async function loadStatus() {
  try {
    const r = await fetch("/api/status");
    const d = await r.json();
    if (!d.ok) return;

    document.getElementById("aiStatusPill").innerHTML = `<span class="dot"></span> هوش مصنوعی: ${d.ai_engine.model}`;

    if (d.active_trader) {
      const t = d.active_trader;
      document.getElementById("copyStatusPill").textContent = `🐋 کپی فعال: ${t.label} (خطا: ${t.consecutive_losses}/2)`;
    }

    const tgPill = document.getElementById("tgStatusPill");
    if (d.telegram_configured) {
      tgPill.innerHTML = `<span class="dot"></span> کانال تلگرام: متصل و آماده ارسال`;
    } else {
      tgPill.innerHTML = `<span class="dot warn"></span> تلگرام: پیش‌نمایش فعال (نیاز به توکن در تنظیمات)`;
    }

    const strip = document.getElementById("tickerStrip");
    strip.innerHTML = "";
    (d.tickers || []).forEach((tk) => {
      const chgCls = tk.change_24h_pct >= 0 ? "up" : "down";
      const sign = tk.change_24h_pct >= 0 ? "+" : "";
      const chip = document.createElement("div");
      chip.className = `ticker-chip ${tk.symbol === activeSymbol ? "active" : ""}`;
      chip.innerHTML = `
        <span class="ticker-sym">${tk.symbol}</span>
        <span class="ltr-num">$${fmtPx(tk.mark_price)}</span>
        <span class="${chgCls} ltr-num" style="font-size:0.78rem;">${sign}${tk.change_24h_pct}%</span>
      `;
      chip.addEventListener("click", () => {
        document.getElementById("symbolInput").value = tk.symbol;
        analyzeSymbol(tk.symbol);
      });
      strip.appendChild(chip);
    });
  } catch (e) {
    console.error("Status error:", e);
  }
}

async function analyzeSymbol(symbolOverride) {
  const sym = (symbolOverride || document.getElementById("symbolInput").value || "BTC").trim().toUpperCase();
  const tf = document.getElementById("timeframeSelect").value;
  activeSymbol = sym;

  const btn = document.getElementById("analyzeBtn");
  btn.disabled = true;
  btn.textContent = "⏳ در حال اجرای ۸ ایجنت...";

  try {
    const r = await fetch(`/api/analyze?symbol=${encodeURIComponent(sym)}&timeframe=${encodeURIComponent(tf)}&send_telegram=false`);
    const data = await r.json();
    if (!data.ok) {
      showToast(`خطا در تحلیل: ${data.error || "نامشخص"}`);
      return;
    }
    currentAnalysis = data;
    renderAnalysis(data);
    loadStatus();
  } catch (e) {
    showToast("خطا در ارتباط با سرور تحلیلگر");
  } finally {
    btn.disabled = false;
    btn.textContent = "🔍 تحلیل عمیق ۸ ایجنت";
  }
}

function renderAnalysis(data) {
  const sig = data.signal;
  document.getElementById("sigPairMeta").textContent = `${sig.pair} • تایم‌فریم ${sig.timeframe} • ${sig.actionable ? "✅ ستاپ تاییدشده معاملاتی" : "⚠️ نیازمند احتیاط / انتظار تایید"}`;
  document.getElementById("sigPriceTitle").textContent = `$${fmtPx(sig.current_price)}`;

  const badge = document.getElementById("sigDirBadge");
  badge.className = `dir-badge dir-${sig.direction}`;
  const dirLabel = sig.direction === "LONG" ? "🟢 LONG (خرید تعهدی)" : sig.direction === "SHORT" ? "🔴 SHORT (فروش تعهدی)" : "⚪️ NEUTRAL (خنثی)";
  badge.textContent = dirLabel;

  document.getElementById("sigConfText").textContent = `اطمینان: ${sig.confidence}% | امتیاز کل: ${sig.composite_score > 0 ? "+" : ""}${sig.composite_score}`;
  document.getElementById("lvlEntry").textContent = `$${fmtPx(sig.entry_low)} - $${fmtPx(sig.entry_high)}`;
  document.getElementById("lvlSL").textContent = `$${fmtPx(sig.stop_loss)}`;
  document.getElementById("lvlLevRR").textContent = `${sig.recommended_leverage}x | R:R 1:${sig.risk_reward_tp2}`;
  document.getElementById("lvlTP1").textContent = `$${fmtPx(sig.tp1)}`;
  document.getElementById("lvlTP2").textContent = `$${fmtPx(sig.tp2)}`;
  document.getElementById("lvlTP3").textContent = `$${fmtPx(sig.tp3)}`;
  document.getElementById("sigExecSummary").textContent = sig.executive_summary_fa;
  document.getElementById("sigInvalidation").textContent = `⚠️ ${sig.invalidation_note_fa}`;

  const srcText = `🧠 بخش مستقل تحلیل بازار (۸ ایجنت خالص — بدون دخالت کپی‌تریدر) • موتور: ${sig.ai_engine_used}`;
  document.getElementById("sigSourceBadge").textContent = srcText;

  // Render S/R Zones, Diagonal Trendlines & Historical Comparison Panel
  const srt = sig.sr_zones_and_trendlines || {};
  const comp = sig.previous_comparison || {};
  const s1z = srt.s1_zone;
  const r1z = srt.r1_zone;
  const ascTl = srt.ascending_tl;
  const descTl = srt.descending_tl;

  document.getElementById("srTrendlineBoxes").innerHTML = `
    <div class="level-box">
      <span class="lbl">🟢 محدوده حمایت اول (S1 Zone)</span>
      <span class="val up ltr-num">${s1z ? `$${fmtPx(s1z.low)} – $${fmtPx(s1z.high)}` : "--"}</span>
      <span style="font-size:0.72rem; color:var(--text-muted); display:block;">${s1z ? `${s1z.touches} برخورد معتبر (${s1z.strength})` : ""}</span>
    </div>
    <div class="level-box">
      <span class="lbl">🔴 محدوده مقاومت اول (R1 Zone)</span>
      <span class="val down ltr-num">${r1z ? `$${fmtPx(r1z.low)} – $${fmtPx(r1z.high)}` : "--"}</span>
      <span style="font-size:0.72rem; color:var(--text-muted); display:block;">${r1z ? `${r1z.touches} برخورد معتبر (${r1z.strength})` : ""}</span>
    </div>
    <div class="level-box">
      <span class="lbl">📉 خط روند نزولی داینامیک</span>
      <span class="val ltr-num">${descTl ? `$${fmtPx(descTl.current_price_on_line)}` : "تشکیل نشده"}</span>
      <span style="font-size:0.72rem; color:#fbbf24; display:block;">${descTl ? descTl.status_fa : "—"}</span>
    </div>
    <div class="level-box">
      <span class="lbl">📈 خط روند صعودی داینامیک</span>
      <span class="val ltr-num">${ascTl ? `$${fmtPx(ascTl.current_price_on_line)}` : "تشکیل نشده"}</span>
      <span style="font-size:0.72rem; color:#38bdf8; display:block;">${ascTl ? ascTl.status_fa : "—"}</span>
    </div>
  `;

  if (comp.has_previous) {
    document.getElementById("compPrevTimeBadge").textContent = `مقایسه‌شده با تحلیل قبلی: ${comp.previous_timestamp}`;
    document.getElementById("compSummaryBox").innerHTML = `
      <b>وضعیت سطوح نسبت به تحلیل قبل:</b> ${comp.sr_transition_fa} &nbsp;|&nbsp;
      <b>تغییر قیمت:</b> <span class="ltr-num">${comp.price_change_pct >= 0 ? "+" : ""}${comp.price_change_pct}%</span> &nbsp;|&nbsp;
      <b>تغییر امتیاز کل ایجنت‌ها:</b> <span class="ltr-num">${comp.previous_score} ⬅️ ${sig.composite_score}</span><br/>
      <b>وضعیت سیگنال قبلی همین ارز:</b> ${comp.prev_signal_status_fa}
    `;
  } else {
    document.getElementById("compPrevTimeBadge").textContent = "ثبت اولین نقطه مرجع (Baseline)";
    document.getElementById("compSummaryBox").textContent =
      "نقطه مرجع اولیه محدوده‌های حمایت/مقاومت و خطوط روند برای این ارز در دیتابیس ذخیره شد؛ از اسکن بعدی تمام شکست‌ها و تبدیل سطوح (S/R Flip) با این تحلیل مقایسه می‌شود.";
  }

  // Render 8 Agent Cards
  const grid = document.getElementById("agentsGrid");
  grid.innerHTML = "";
  (sig.agent_reports || []).forEach((rep) => {
    const score = Number(rep.score || 0);
    const pctWidth = Math.min(50, Math.abs(score) / 2);
    const leftPos = score >= 0 ? 50 : 50 - pctWidth;
    const fillCol = score >= 14 ? "var(--long)" : score <= -14 ? "var(--short)" : "var(--warn)";
    const tagCol = rep.direction === "LONG" ? "up" : rep.direction === "SHORT" ? "down" : "neutral-txt";

    const card = document.createElement("div");
    card.className = "agent-card";
    const bulletsHtml = (rep.bullets_fa || []).map((b) => `<li>${b}</li>`).join("");
    card.innerHTML = `
      <div>
        <div class="agent-top">
          <div>
            <div class="agent-name">${rep.agent_name_fa}</div>
            <div class="agent-cat">${rep.category_fa}</div>
          </div>
          <span class="${tagCol} ltr-num" style="font-weight:800; font-size:0.86rem;">
            ${rep.direction} (${score > 0 ? "+" : ""}${score})
          </span>
        </div>
        <div class="score-meter">
          <div class="score-fill" style="left:${leftPos}%; width:${pctWidth}%; background:${fillCol};"></div>
        </div>
        <div class="agent-summary">${rep.summary_fa}</div>
      </div>
      <ul class="agent-bullets">${bulletsHtml}</ul>
    `;
    grid.appendChild(card);
  });

  // Derivatives & Fundamentals quick boxes
  const deriv = data.derivatives || {};
  const fund = data.fundamentals || {};
  const oiM = ((deriv.total_open_interest_usd || 0) / 1e6).toFixed(1);
  const fPct = ((deriv.avg_funding_8h || 0) * 100).toFixed(4);
  document.getElementById("derivQuickStats").textContent = `OI کل: $${oiM}M | فاندینگ ۸ ساعته: ${fPct}%`;

  const obImb = ((deriv.orderbook?.imbalance || 0) * 100).toFixed(1);
  document.getElementById("derivDetailGrid").innerHTML = `
    <div class="level-box"><span class="lbl">Open Interest تجمیعی</span><span class="val ltr-num">$${oiM}M</span></div>
    <div class="level-box"><span class="lbl">فاندینگ ریت میانگین (8h)</span><span class="val ltr-num">${fPct}%</span></div>
    <div class="level-box"><span class="lbl">عدم تقارن اردربوک L2</span><span class="val ${obImb >= 0 ? "up" : "down"} ltr-num">${obImb}%</span></div>
    <div class="level-box"><span class="lbl">شاخص ترس و طمع بازار</span><span class="val ltr-num">${fund.fear_greed_value || 50} (${fund.fear_greed_label || "Neutral"})</span></div>
    <div class="level-box"><span class="lbl">دامیننس بیت‌کوین</span><span class="val ltr-num">${fund.btc_dominance || "--"}%</span></div>
    <div class="level-box"><span class="lbl">حجم ۲۴ ساعته مشتقات</span><span class="val ltr-num">$${((deriv.volume_24h_usd || 0) / 1e6).toFixed(1)}M</span></div>
  `;

  // Live News
  const newsBox = document.getElementById("liveNewsList");
  const newsArr = data.news || [];
  if (!newsArr.length) {
    newsBox.textContent = "خبر مرتبطی در این لحظه یافت نشد.";
  } else {
    newsBox.innerHTML = newsArr
      .slice(0, 6)
      .map(
        (n) => `
        <div style="padding: 7px 0; border-bottom: 1px solid rgba(30,45,74,0.5); direction:ltr; text-align:left;">
          <span style="color:var(--accent); font-weight:700; font-size:0.74rem;">[${n.source}]</span>
          <a href="${n.url}" target="_blank" rel="noopener" style="color:#e2e8f0; text-decoration:none; margin-left:5px;">${n.title}</a>
        </div>`
      )
      .join("");
  }

  drawCandlestickChart(data);
}

function drawCandlestickChart(data) {
  const canvas = document.getElementById("priceChart");
  if (!canvas) return;
  const rect = canvas.parentElement.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);

  const W = rect.width;
  const H = rect.height;
  ctx.clearRect(0, 0, W, H);

  const candles = data.chart_candles || [];
  if (!candles.length) return;
  const sig = data.signal;
  const srRep = (sig.agent_reports || []).find((r) => r.agent_id === "support_resistance");
  const s1 = srRep?.metrics?.s1;
  const r1 = srRep?.metrics?.r1;

  let minP = Math.min(...candles.map((c) => c.l), sig.stop_loss, sig.tp1, sig.tp2);
  let maxP = Math.max(...candles.map((c) => c.h), sig.stop_loss, sig.tp1, sig.tp2);
  const pad = (maxP - minP) * 0.08 || 1;
  minP -= pad;
  maxP += pad;

  const padRight = 76;
  const padLeft = 14;
  const padTop = 14;
  const padBot = 22;
  const plotW = W - padLeft - padRight;
  const plotH = H - padTop - padBot;

  const yOf = (p) => padTop + ((maxP - p) / (maxP - minP)) * plotH;

  // Grid lines
  ctx.strokeStyle = "rgba(30, 45, 74, 0.55)";
  ctx.lineWidth = 1;
  ctx.font = "11px monospace";
  ctx.fillStyle = "#64748b";
  for (let i = 0; i <= 5; i++) {
    const p = minP + ((maxP - minP) * i) / 5;
    const y = yOf(p);
    ctx.beginPath();
    ctx.moveTo(padLeft, y);
    ctx.lineTo(W - padRight, y);
    ctx.stroke();
    ctx.fillText(fmtPx(p), W - padRight + 6, y + 4);
  }

  // Entry band
  const yEntryTop = yOf(sig.entry_high);
  const yEntryBot = yOf(sig.entry_low);
  ctx.fillStyle = "rgba(56, 189, 248, 0.13)";
  ctx.fillRect(padLeft, yEntryTop, plotW, Math.max(3, yEntryBot - yEntryTop));

  function drawHoriz(price, color, dash, label) {
    if (!price || price < minP || price > maxP) return;
    const y = yOf(price);
    ctx.save();
    ctx.strokeStyle = color;
    ctx.setLineDash(dash);
    ctx.lineWidth = 1.3;
    ctx.beginPath();
    ctx.moveTo(padLeft, y);
    ctx.lineTo(W - padRight, y);
    ctx.stroke();
    ctx.fillStyle = color;
    ctx.font = "bold 10px monospace";
    ctx.fillText(label, padLeft + 6, y - 4);
    ctx.restore();
  }

  drawHoriz(sig.stop_loss, "#f43f5e", [5, 4], `SL ${fmtPx(sig.stop_loss)}`);
  drawHoriz(sig.tp1, "#10b981", [5, 4], `TP1 ${fmtPx(sig.tp1)}`);
  drawHoriz(sig.tp2, "#34d399", [3, 3], `TP2 ${fmtPx(sig.tp2)}`);

  // Shaded Support & Resistance Zones (S1 Zone & R1 Zone)
  const srt = sig.sr_zones_and_trendlines || {};
  if (srt.s1_zone) {
    const yTop = yOf(srt.s1_zone.high);
    const yBot = yOf(srt.s1_zone.low);
    ctx.fillStyle = "rgba(16, 185, 129, 0.14)";
    ctx.fillRect(padLeft, yTop, plotW, Math.max(4, yBot - yTop));
    drawHoriz(srt.s1_zone.mid, "#10b981", [2, 4], `S1 Zone`);
  } else if (s1) {
    drawHoriz(s1, "#a855f7", [2, 4], `S1`);
  }

  if (srt.r1_zone) {
    const yTop = yOf(srt.r1_zone.high);
    const yBot = yOf(srt.r1_zone.low);
    ctx.fillStyle = "rgba(168, 85, 247, 0.16)";
    ctx.fillRect(padLeft, yTop, plotW, Math.max(4, yBot - yTop));
    drawHoriz(srt.r1_zone.mid, "#a855f7", [2, 4], `R1 Zone`);
  } else if (r1) {
    drawHoriz(r1, "#a855f7", [2, 4], `R1`);
  }

  const step = plotW / candles.length;
  function drawDiagonalTrendline(tl, color, label) {
    if (!tl || !tl.t1 || !tl.t2) return;
    // Map timestamps t1 and t2 onto candle x coordinates
    let idx1 = candles.findIndex((c) => c.t >= tl.t1);
    if (idx1 < 0) idx1 = 0;
    const idx2 = candles.length - 1;
    // Interpolate p1 if tl.t1 started slightly before visible 75-candle window
    let pStart = tl.p1;
    if (candles[0].t > tl.t1 && tl.t2 > tl.t1) {
      const ratio = (candles[0].t - tl.t1) / (tl.t2 - tl.t1);
      pStart = tl.p1 + (tl.p2 - tl.p1) * ratio;
      idx1 = 0;
    }
    const x1 = padLeft + idx1 * step + step / 2;
    const x2 = padLeft + idx2 * step + step / 2;
    const y1 = yOf(pStart);
    const y2 = yOf(tl.p2);

    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = 2.0;
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
    ctx.fillStyle = color;
    ctx.font = "bold 10px monospace";
    ctx.fillText(label, Math.max(padLeft + 8, x2 - 95), y2 - 6);
    ctx.restore();
  }

  drawDiagonalTrendline(srt.descending_tl, "#f59e0b", "Desc Trendline");
  drawDiagonalTrendline(srt.ascending_tl, "#38bdf8", "Asc Trendline");

  // Candles
  const bodyW = Math.max(2.5, step * 0.64);
  candles.forEach((c, idx) => {
    const x = padLeft + idx * step + step / 2;
    const isUp = c.c >= c.o;
    ctx.strokeStyle = isUp ? "#10b981" : "#f43f5e";
    ctx.fillStyle = isUp ? "#10b981" : "#f43f5e";
    ctx.lineWidth = 1.2;

    ctx.beginPath();
    ctx.moveTo(x, yOf(c.h));
    ctx.lineTo(x, yOf(c.l));
    ctx.stroke();

    const yO = yOf(c.o);
    const yC = yOf(c.c);
    const top = Math.min(yO, yC);
    const h = Math.max(1.5, Math.abs(yC - yO));
    ctx.fillRect(x - bodyW / 2, top, bodyW, h);
  });
}

// Copy Trader View
async function loadCopyTraders() {
  try {
    const r = await fetch("/api/copy-traders");
    const d = await r.json();
    if (!d.ok) return;
    renderCopyTraders(d);
  } catch (e) {
    console.error(e);
  }
}

function renderCopyTraders(d) {
  const active = d.active_trader;
  const maxL = d.max_consecutive_losses || 2;

  if (active) {
    document.getElementById("ctActiveName").textContent = active.label;
    document.getElementById("ctActiveAddr").textContent = active.address;
    document.getElementById("ctActiveEq").textContent = `$${Number(active.account_value_usd).toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
    document.getElementById("ctActiveRoi").textContent = `+${active.roi_month_pct}%`;
    document.getElementById("ctActivePnl").textContent = `+$${Number(active.pnl_month_usd).toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
    document.getElementById("ctActiveWinRate").textContent = `${active.win_rate_pct}%`;

    const sBox = document.getElementById("strikeBoxes");
    sBox.innerHTML = "";
    for (let i = 1; i <= maxL; i++) {
      const hit = active.consecutive_losses >= i;
      const div = document.createElement("div");
      div.className = `strike-slot ${hit ? "filled" : ""}`;
      div.textContent = hit
        ? `❌ خطای متوالی شماره ${i} ثبت شد!`
        : `خطای متوالی شماره ${i}: خالی (ایمن)`;
      sBox.appendChild(div);
    }

    const posBody = document.getElementById("activePositionsBody");
    const positions = active.open_positions || [];
    if (!positions.length) {
      posBody.innerHTML = `<tr><td colspan="7" style="color:var(--text-muted);">این نهنگ در حال حاضر پوزیشن فیوچرز بازی ندارد.</td></tr>`;
    } else {
      posBody.innerHTML = positions
        .slice(0, 10)
        .map(
          (p) => `
          <tr>
            <td><b>${p.coin}</b></td>
            <td class="${p.direction === "LONG" ? "up" : "down"} ltr-num"><b>${p.direction}</b></td>
            <td class="ltr-num">$${Number(p.position_value_usd).toLocaleString("en-US", { maximumFractionDigits: 0 })}</td>
            <td class="ltr-num">$${fmtPx(p.entry_price)}</td>
            <td class="ltr-num">${p.leverage}x</td>
            <td class="${p.unrealized_pnl_usd >= 0 ? "up" : "down"} ltr-num">${p.unrealized_pnl_usd >= 0 ? "+" : ""}$${Number(p.unrealized_pnl_usd).toLocaleString("en-US", { maximumFractionDigits: 0 })}</td>
            <td><button class="btn btn-success" style="padding:4px 10px; font-size:0.75rem;" onclick="sendCopyPositionSignal('${p.coin}')">🐋 ارسال سیگنال کپی</button></td>
          </tr>`
        )
        .join("");
    }
  }

  // Rotation logs
  const rotBox = document.getElementById("rotationLogsBox");
  const rots = d.rotations || [];
  if (!rots.length) {
    rotBox.innerHTML = `<div style="padding:10px; background:rgba(16,185,129,0.08); border-radius:8px;">✅ هنوز هیچ تریدری به سقف ۲ اشتباه متوالی نرسیده است.</div>`;
  } else {
    rotBox.innerHTML = rots
      .map(
        (rt) => `
        <div style="padding:9px 11px; border:1px solid var(--border); border-radius:9px; margin-bottom:8px; background:rgba(244,63,94,0.06);">
          <div style="font-weight:700; color:#fda4af;">🔄 حذف خودکار ${rt.old_trader_label} (${rt.consecutive_losses} خطای پیاپی)</div>
          <div style="margin-top:3px;">✅ جایگزین جدید: <b style="color:#34d399;">${rt.new_trader_label || "—"}</b></div>
          <div style="font-size:0.74rem; color:var(--text-muted); margin-top:3px;">علت: ${rt.reason_fa} • <span class="ltr-num">${rt.timestamp_iso}</span></div>
        </div>`
      )
      .join("");
  }

  // Roster Table
  const tbody = document.getElementById("rosterTableBody");
  tbody.innerHTML = (d.traders || [])
    .map((t) => {
      const stBadge =
        t.status === "ACTIVE"
          ? `<span class="up" style="font-weight:700;">🟢 در حال کپی</span>`
          : t.status === "STANDBY"
          ? `<span style="color:var(--accent);">⏳ آماده جایگزینی</span>`
          : `<span class="down">❌ حذف‌شده (۲ خطا)</span>`;
      return `
        <tr>
          <td class="ltr-num">#${t.rank}</td>
          <td>
            <b>${t.label}</b>
            <div class="ltr-num" style="font-size:0.72rem; color:var(--text-muted);">${t.address.slice(0, 10)}...${t.address.slice(-6)}</div>
          </td>
          <td>${stBadge}</td>
          <td class="ltr-num">$${Number(t.account_value_usd).toLocaleString("en-US", { maximumFractionDigits: 0 })}</td>
          <td class="up ltr-num">+$${Number(t.pnl_month_usd).toLocaleString("en-US", { maximumFractionDigits: 0 })}</td>
          <td class="up ltr-num">+${t.roi_month_pct}%</td>
          <td class="ltr-num">${t.win_rate_pct}%</td>
          <td class="ltr-num">${t.consecutive_losses} / ${maxL}</td>
          <td class="ltr-num">${(t.open_positions || []).length} پوزیشن</td>
          <td>
            ${
              t.status !== "ACTIVE"
                ? `<button class="btn" style="padding:4px 10px; font-size:0.75rem;" onclick="activateTrader('${t.address}')">انتخاب به عنوان فعال</button>`
                : `—`
            }
          </td>
        </tr>`;
    })
    .join("");
}

window.jumpAnalyze = function (coin) {
  document.querySelector('[data-tab="terminal"]').click();
  document.getElementById("symbolInput").value = coin;
  analyzeSymbol(coin);
};

window.sendCopyPositionSignal = async function (coin) {
  const r = await fetch("/api/copy-traders/send-signal", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ coin: coin || null }),
  });
  const d = await r.json();
  if (!d.ok) {
    showToast(d.error || "پوزیشن بازی برای ارسال یافت نشد.");
    return;
  }
  showToast(`🐋 سیگنال مستقل کپی‌تریدر (${(d.signals || []).map((s) => s.symbol).join(", ")}) به تلگرام ارسال شد!`);
};

document.getElementById("emitAllCopySigBtn").addEventListener("click", () => {
  window.sendCopyPositionSignal(null);
});

window.activateTrader = async function (address) {
  const r = await fetch("/api/copy-traders/activate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ address }),
  });
  const d = await r.json();
  if (d.ok) {
    showToast("تریدر انتخابی به عنوان تریدر مرجع فعال شد.");
    loadCopyTraders();
    loadStatus();
  }
};

document.getElementById("simLossBtn").addEventListener("click", async () => {
  const r = await fetch("/api/copy-traders/outcome", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ outcome: "LOSS", note: "بسته شدن پوزیشن کپی‌شده با حد ضرر (تست شمارنده خطا)" }),
  });
  const d = await r.json();
  showToast(d.message_fa || "ثبت شد");
  renderCopyTraders({
    active_trader: d.active_trader,
    traders: d.traders,
    rotations: d.rotations,
    max_consecutive_losses: 2,
  });
  loadStatus();
});

document.getElementById("simWinBtn").addEventListener("click", async () => {
  const r = await fetch("/api/copy-traders/outcome", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ outcome: "WIN" }),
  });
  const d = await r.json();
  showToast(d.message_fa || "ثبت شد");
  renderCopyTraders({
    active_trader: d.active_trader,
    traders: d.traders,
    rotations: d.rotations,
    max_consecutive_losses: 2,
  });
  loadStatus();
});

document.getElementById("refreshLeaderboardBtn").addEventListener("click", async (ev) => {
  ev.target.disabled = true;
  ev.target.textContent = "⏳ در حال دریافت از Hyperliquid...";
  try {
    const r = await fetch("/api/copy-traders/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force_reseed: true }),
    });
    const d = await r.json();
    if (d.ok) {
      renderCopyTraders({ ...d, max_consecutive_losses: 2 });
      showToast("لیست تریدرهای برتر و پوزیشن‌های باز بروزرسانی شد.");
    }
  } finally {
    ev.target.disabled = false;
    ev.target.textContent = "🔄 بروزرسانی زنده لیدربورد";
  }
});

// Send current signal to Telegram
document.getElementById("sendCurrentSigBtn").addEventListener("click", async () => {
  if (!currentAnalysis?.signal) return;
  const r = await fetch("/api/telegram/send-signal", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ signal_id: currentAnalysis.signal.signal_id, signal: currentAnalysis.signal }),
  });
  const d = await r.json();
  showToast(d.note || "پیام پردازش شد.");
});

// Watchlist Scan
document.getElementById("scanAllBtn").addEventListener("click", async (ev) => {
  const btn = ev.currentTarget;
  btn.disabled = true;
  btn.textContent = "⏳ اسکن کل واچ‌لیست...";
  try {
    const r = await fetch("/api/scan-watchlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const d = await r.json();
    if (d.ok) {
      const actionableCnt = (d.signals || []).filter((s) => s.actionable).length;
      showToast(`اسکن ${d.count} ارز تکمیل شد (${actionableCnt} سیگنال تاییدشده).`);
      document.querySelector('[data-tab="telegram"]').click();
    }
  } finally {
    btn.disabled = false;
    btn.textContent = "📡 اسکن کل واچ‌لیست";
  }
});

async function loadTelegramLogs() {
  const r = await fetch("/api/telegram/logs");
  const d = await r.json();
  const box = document.getElementById("tgFeedBox");
  const logs = d.logs || [];
  if (!logs.length && currentAnalysis?.telegram_preview) {
    box.innerHTML = `<div class="tg-bubble">${currentAnalysis.telegram_preview}</div>`;
    return;
  }
  if (!logs.length) {
    box.innerHTML = `<div style="color:var(--text-muted);">هنوز پیامی ثبت نشده است. روی «ارسال این سیگنال به کانال تلگرام» کلیک کنید تا قالب پیام را ببینید.</div>`;
    return;
  }
  box.innerHTML = logs
    .map(
      (lg) => `
      <div class="tg-bubble">
        <div style="font-size:0.73rem; color:${lg.delivered ? "#34d399" : "#fbbf24"}; margin-bottom:6px; border-bottom:1px solid rgba(255,255,255,0.08); padding-bottom:4px;">
          ${lg.delivered ? "✅ ارسال‌شده به تلگرام" : "👁 پیش‌نمایش آماده ارسال"} • ${lg.delivery_note}
        </div>
        <div>${lg.message_text}</div>
      </div>`
    )
    .join("");
}

async function loadSignalsArchive() {
  const onlyAct = document.getElementById("actionableOnlyChk").checked;
  const srcFilter = document.getElementById("signalSourceFilter")?.value || "";
  const r = await fetch(`/api/signals?limit=30&actionable_only=${onlyAct}&source=${encodeURIComponent(srcFilter)}`);
  const d = await r.json();
  const tbody = document.getElementById("signalsHistoryBody");
  const sigs = d.signals || [];
  if (!sigs.length) {
    tbody.innerHTML = `<tr><td colspan="9" style="color:var(--text-muted);">سیگنالی ثبت نشده است.</td></tr>`;
    return;
  }
  tbody.innerHTML = sigs
    .map((s) => {
      const isCopy = s.signal_source === "COPY_TRADER";
      const srcBadge = isCopy
        ? `<span style="color:#38bdf8; font-weight:700; font-size:0.75rem;">🐋 کپی‌تریدر</span>`
        : `<span style="color:#a855f7; font-weight:700; font-size:0.75rem;">🧠 تحلیل بازار</span>`;
      return `
      <tr>
        <td class="ltr-num" style="font-size:0.74rem;">${s.timestamp_iso.slice(5, 16)}</td>
        <td>${srcBadge}</td>
        <td><b>${s.symbol}</b> <span style="font-size:0.72rem; color:var(--text-muted);">(${s.timeframe})</span></td>
        <td class="${s.direction === "LONG" ? "up" : s.direction === "SHORT" ? "down" : "neutral-txt"} ltr-num"><b>${s.direction}</b></td>
        <td class="ltr-num">${s.confidence}%</td>
        <td class="ltr-num">$${fmtPx(s.current_price)}</td>
        <td class="down ltr-num">$${fmtPx(s.stop_loss)}</td>
        <td class="up ltr-num">$${fmtPx(s.tp2)}</td>
        <td>
          <button class="btn" style="padding:3px 9px; font-size:0.74rem;" onclick="resendSignal('${s.signal_id}')">
            ${s.telegram_sent ? "✅ ارسال مجدد" : "✈️ ارسال"}
          </button>
        </td>
      </tr>`;
    })
    .join("");
}

window.resendSignal = async function (signalId) {
  const r = await fetch("/api/telegram/send-signal", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ signal_id: signalId }),
  });
  const d = await r.json();
  showToast(d.note || "پردازش شد");
  loadTelegramLogs();
  loadSignalsArchive();
};

document.getElementById("actionableOnlyChk").addEventListener("change", loadSignalsArchive);
document.getElementById("signalSourceFilter")?.addEventListener("change", loadSignalsArchive);

document.getElementById("testTgQuickBtn").addEventListener("click", async () => {
  const r = await fetch("/api/telegram/test", { method: "POST" });
  const d = await r.json();
  showToast(d.note || "تست انجام شد");
  loadTelegramLogs();
});

// Settings
const SETTING_FIELDS = [
  "TELEGRAM_BOT_TOKEN",
  "TELEGRAM_CHAT_ID",
  "TELEGRAM_AUTO_SEND",
  "WATCHLIST",
  "PRIMARY_TIMEFRAME",
  "HTF_TIMEFRAME",
  "LTF_TIMEFRAME",
  "MAX_LEVERAGE",
  "MIN_CONFIDENCE_SCORE",
  "MIN_RISK_REWARD_RATIO",
  "COPY_TRADER_MAX_CONSECUTIVE_LOSSES",
  "COPY_TRADER_MIN_EQUITY_USD",
  "AUTO_SCAN_ENABLED",
  "AUTO_SCAN_INTERVAL_SEC",
  "AI_PROVIDER",
  "GEMINI_API_KEY",
  "OPENROUTER_API_KEY",
  "OPENAI_API_KEY",
];

async function loadSettings() {
  const r = await fetch("/api/settings");
  const d = await r.json();
  if (!d.ok) return;
  const s = d.settings;
  SETTING_FIELDS.forEach((k) => {
    const el = document.getElementById(`cfg_${k}`);
    if (el && s[k] !== undefined) {
      el.value = String(s[k]);
    }
  });
}

async function saveSettingsPayload() {
  const payload = {};
  SETTING_FIELDS.forEach((k) => {
    const el = document.getElementById(`cfg_${k}`);
    if (el) payload[k] = el.value;
  });
  const r = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return await r.json();
}

document.getElementById("settingsForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const d = await saveSettingsPayload();
  if (d.ok) {
    showToast("✅ تنظیمات با موفقیت در سیستم و فایل .env ذخیره شد.");
    loadStatus();
  }
});

document.getElementById("saveAndTestTgBtn").addEventListener("click", async () => {
  await saveSettingsPayload();
  const r = await fetch("/api/telegram/test", { method: "POST" });
  const d = await r.json();
  showToast(d.note || "تست تلگرام انجام شد.");
  loadStatus();
});

document.getElementById("detectChatIdBtn").addEventListener("click", async () => {
  const tok = document.getElementById("cfg_TELEGRAM_BOT_TOKEN").value;
  const box = document.getElementById("detectedChatsList");
  box.textContent = "⏳ در حال بررسی آخرین پیام‌ها و کانال/گروه‌های متصل به ربات...";
  const r = await fetch("/api/telegram/detect-chats", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ bot_token: tok }),
  });
  const d = await r.json();
  if (!d.ok) {
    box.innerHTML = `<span class="down">${d.error}</span>`;
    return;
  }
  const chats = d.chats || [];
  if (!chats.length) {
    box.innerHTML = `<span class="neutral-txt">موردی یافت نشد. ابتدا ربات را در کانال/گروه ادمین کنید و یک پیام (مثلاً «تست») داخل آن کانال/گروه بفرستید، سپس دوباره کلیک کنید.</span>`;
    return;
  }
  box.innerHTML = chats
    .map(
      (c) =>
        `<button type="button" class="btn" style="padding:3px 9px; font-size:0.75rem; margin:3px 0 0 6px;" onclick="document.getElementById('cfg_TELEGRAM_CHAT_ID').value='${c.numeric_id}'">انتخاب ${c.title} (${c.type}: ${c.numeric_id})</button>`
    )
    .join("");
});

document.getElementById("analyzeBtn").addEventListener("click", () => analyzeSymbol());
document.getElementById("symbolInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") analyzeSymbol();
});

window.addEventListener("resize", () => {
  if (currentAnalysis) drawCandlestickChart(currentAnalysis);
});

// Initial boot
(async function init() {
  await loadStatus();
  await analyzeSymbol("BTC");
})();
