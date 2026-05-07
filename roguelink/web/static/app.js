/* RogueLink Dashboard — global API action handler.
 *
 * Intercepts ALL form submissions with data-api attribute via a single
 * document-level "submit" listener (event delegation). This ensures even
 * forms rendered dynamically or inside <details> elements are caught.
 *
 * Also handles standalone <button data-api="..."> clicks via a document-level
 * "click" listener.
 *
 * Forms MUST have:
 *   data-api="/api/endpoint"    — the fetch URL
 *   action="javascript:void(0)" — safety net to prevent native navigation
 *
 * Optional attributes:
 *   data-method="POST"          — HTTP method (default: POST)
 *   data-render="scan"          — custom renderer name
 *   data-result="#element-id"   — explicit result target
 *   data-reload="true"          — reload page after success
 */

(function () {
  "use strict";

  // -----------------------------------------------------------------------
  // Helpers
  // -----------------------------------------------------------------------

  function qs(sel, root) {
    return (root || document).querySelector(sel);
  }

  function ce(tag, attrs, text) {
    var el = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) { el.setAttribute(k, attrs[k]); });
    }
    if (text !== undefined) el.textContent = text;
    return el;
  }

  function escHtml(s) {
    if (s === null || s === undefined) return "";
    var d = document.createElement("div");
    d.textContent = String(s);
    return d.innerHTML;
  }

  /** Find the result target for a form or button element. */
  function findResultTarget(el) {
    // 1. Explicit data-result="#id"
    var rid = el.getAttribute("data-result");
    if (rid) {
      var explicit = qs(rid);
      if (explicit) return explicit;
    }
    // 2. Next sibling .api-result
    var sib = el.nextElementSibling;
    while (sib) {
      if (sib.classList && sib.classList.contains("api-result")) return sib;
      sib = sib.nextElementSibling;
    }
    // 3. Parent's next sibling .api-result
    var parent = el.parentElement;
    if (parent) {
      sib = parent.nextElementSibling;
      while (sib) {
        if (sib.classList && sib.classList.contains("api-result")) return sib;
        sib = sib.nextElementSibling;
      }
    }
    // 4. Fallback: create one after the element
    var div = ce("div", { "class": "api-result" });
    if (el.parentNode) {
      el.parentNode.insertBefore(div, el.nextSibling);
    }
    return div;
  }

  // -----------------------------------------------------------------------
  // Renderers
  // -----------------------------------------------------------------------

  function renderLoading(target) {
    target.className = "api-result loading";
    target.innerHTML = '<span class="spinner"></span> Working\u2026';
  }

  function renderSuccess(target, data) {
    target.className = "api-result success";
    if (typeof data === "string") {
      target.textContent = data;
      return;
    }
    var html = "";
    if (data.ok === true) html += '<p class="api-ok">\u2713 OK</p>';
    if (data.ok === false) html += '<p class="api-err">\u2717 Failed</p>';
    if (data.detail) html += '<p class="api-err">' + escHtml(data.detail) + "</p>";
    if (data.error) html += '<p class="api-err">' + escHtml(data.error) + "</p>";
    if (data.message) html += "<p>" + escHtml(data.message) + "</p>";
    var skip = new Set([
      "ok", "error", "message", "detail", "raw", "output",
      "public_targets", "dns_targets", "gateway_ping", "dns_servers", "wan_signal"
    ]);
    Object.keys(data).forEach(function (k) {
      if (skip.has(k)) return;
      var v = data[k];
      if (v === null || v === undefined) return;
      if (typeof v === "object") {
        html += "<p><strong>" + escHtml(k) + ":</strong> <code>" + escHtml(JSON.stringify(v)) + "</code></p>";
      } else {
        html += "<p><strong>" + escHtml(k) + ":</strong> " + escHtml(String(v)) + "</p>";
      }
    });
    target.innerHTML = html || "<p>Done.</p>";
  }

  function renderError(target, message) {
    target.className = "api-result error";
    target.innerHTML = '<p class="api-err">\u2717 ' + escHtml(message) + "</p>";
  }

  /** Render Wi-Fi scan results as a table. */
  function renderScanTable(target, data) {
    target.className = "api-result success";
    var networks = data.networks || [];
    if (!networks.length) {
      target.innerHTML = "<p>No networks found.</p>";
      return;
    }
    var cols = ["SSID", "BSSID", "Signal dBm", "Quality", "Channel", "Band", "Security", "Interface", "Actions"];
    var table = ce("table");
    var thead = ce("thead");
    var tr = ce("tr");
    cols.forEach(function (c) { tr.appendChild(ce("th", null, c)); });
    thead.appendChild(tr);
    table.appendChild(thead);
    var tbody = ce("tbody");
    networks.forEach(function (n) {
      var row = ce("tr");
      row.appendChild(ce("td", null, n.ssid || ""));
      row.appendChild(ce("td", null, n.bssid || ""));
      var sig = n.signal_dbm;
      row.appendChild(ce("td", null, sig != null ? Math.round(sig).toString() : "\u2014"));
      row.appendChild(ce("td", null, n.quality || "\u2014"));
      row.appendChild(ce("td", null, n.channel != null ? String(n.channel) : "\u2014"));
      row.appendChild(ce("td", null, n.band || "\u2014"));
      row.appendChild(ce("td", null, n.security || "\u2014"));
      row.appendChild(ce("td", null, n.iface || "\u2014"));
      var actTd = ce("td");
      if (n.ssid) {
        var saveBtn = ce("button", { "class": "btn-sm", type: "button" }, "Save");
        saveBtn.addEventListener("click", function () { openSaveDialog(n); });
        actTd.appendChild(saveBtn);
      }
      row.appendChild(actTd);
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    target.innerHTML = "";
    var heading = ce("p", null, networks.length + " network(s) found on " + (data.iface || "\u2014"));
    heading.style.marginBottom = "8px";
    target.appendChild(heading);
    target.appendChild(table);
  }

  /** Render health check result. */
  function renderHealth(target, data) {
    target.className = "api-result success";
    var html = "";
    var colors = { excellent: "#4ade80", good: "#4ade80", partial: "#fbbf24", weak: "#fbbf24", unstable: "#fbbf24", offline: "#f87171" };
    var st = data.status || data.overall || "unknown";
    var c = colors[st] || "#d6dde5";
    html += '<p><strong>Overall:</strong> <span style="color:' + c + '">' + escHtml(st) + "</span></p>";
    if (data.reason) html += "<p><strong>Reason:</strong> " + escHtml(data.reason) + "</p>";
    var s = data.summary || {};
    if (s.rtt_ms != null) html += "<p><strong>RTT:</strong> " + s.rtt_ms + " ms</p>";
    if (s.packet_loss_pct != null) html += "<p><strong>Packet loss:</strong> " + s.packet_loss_pct + "%</p>";
    if (s.gateway) html += "<p><strong>Gateway:</strong> " + escHtml(s.gateway) + "</p>";
    if (s.wan_iface) html += "<p><strong>WAN iface:</strong> " + escHtml(s.wan_iface) + "</p>";
    if (s.signal_dbm != null) html += "<p><strong>Signal:</strong> " + s.signal_dbm + " dBm</p>";
    if (data.dns_ok !== undefined) html += "<p><strong>DNS:</strong> " + (data.dns_ok ? "OK" : "Failing") + "</p>";
    if (data.management_internet !== undefined)
      html += "<p><strong>Mgmt Internet:</strong> " + (data.management_internet ? "OK" : "No") + "</p>";
    if (data.wan_status) html += "<p><strong>WAN status:</strong> " + escHtml(data.wan_status) + "</p>";
    if (data.duration_s != null) html += '<p class="muted">Checked in ' + data.duration_s + "s</p>";
    target.innerHTML = html;
  }

  /** Render speedtest result. */
  function renderSpeedtest(target, data) {
    target.className = "api-result success";
    var html = "";
    if (data.ok === false) {
      html += '<p class="api-err">\u2717 ' + escHtml(data.error || "Speed test failed") + "</p>";
    } else {
      html += "<p><strong>Download:</strong> " + (data.download_mbps || "\u2014") + " Mbps</p>";
      html += "<p><strong>Upload:</strong> " + (data.upload_mbps || "\u2014") + " Mbps</p>";
      html += "<p><strong>Ping:</strong> " + (data.ping_ms || "\u2014") + " ms</p>";
      if (data.server_name) html += "<p><strong>Server:</strong> " + escHtml(data.server_name) + "</p>";
    }
    target.innerHTML = html;
  }

  /** Render WAN scan results table. */
  function renderWanScan(target, data) {
    target.className = "api-result success";
    var networks = data.networks || [];
    if (!networks.length) {
      target.innerHTML = "<p>No networks found.</p>";
      return;
    }
    var table = ce("table");
    var thead = ce("thead");
    var tr = ce("tr");
    ["SSID", "BSSID", "Channel", "Signal", "Encryption"].forEach(function (c) {
      tr.appendChild(ce("th", null, c));
    });
    thead.appendChild(tr);
    table.appendChild(thead);
    var tbody = ce("tbody");
    networks.forEach(function (n) {
      var row = ce("tr");
      row.appendChild(ce("td", null, n.ssid || ""));
      row.appendChild(ce("td", null, n.bssid || ""));
      row.appendChild(ce("td", null, n.channel != null ? String(n.channel) : "\u2014"));
      row.appendChild(ce("td", null, n.signal != null ? String(n.signal) : "\u2014"));
      row.appendChild(ce("td", null, n.encryption || "\u2014"));
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    target.innerHTML = "";
    target.appendChild(ce("p", null, networks.length + " network(s) found."));
    target.appendChild(table);
  }

  function openSaveDialog(network) {
    var ssid = network.ssid || "";
    var psk = prompt('PSK for "' + ssid + '" (leave empty for open):', "");
    if (psk === null) return;
    var note = prompt("Note (optional):", "");
    if (note === null) note = "";
    var body = new FormData();
    body.append("ssid", ssid);
    body.append("psk", psk);
    body.append("note", note);
    fetch("/api/networks/saved", { method: "POST", body: body })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.ok || d.id) alert("Saved: " + ssid);
        else alert("Error: " + (d.error || d.detail || "unknown"));
      })
      .catch(function (e) { alert("Network error: " + e.message); });
  }

  // -----------------------------------------------------------------------
  // Custom renderer registry
  // -----------------------------------------------------------------------

  var renderers = {
    scan: renderScanTable,
    health: renderHealth,
    speedtest: renderSpeedtest,
    wanscan: renderWanScan
  };

  // -----------------------------------------------------------------------
  // Core: document-level submit handler (event delegation)
  // -----------------------------------------------------------------------

  function handleFormSubmit(ev) {
    // Walk up from ev.target to find the form
    var form = ev.target;
    if (form.tagName !== "FORM") form = form.closest("form");
    if (!form) return;
    var url = form.getAttribute("data-api");
    if (!url) return; // Not an API form, let it submit normally

    // ALWAYS prevent default — never navigate away
    ev.preventDefault();
    ev.stopPropagation();

    var method = (form.getAttribute("data-method") || form.method || "POST").toUpperCase();
    var target = findResultTarget(form);
    var renderName = form.getAttribute("data-render");
    var reload = form.getAttribute("data-reload") === "true";

    renderLoading(target);

    // Disable submit buttons during request
    var btns = form.querySelectorAll("button");
    btns.forEach(function (b) { b.disabled = true; });

    var opts = { method: method };
    if (method === "POST" || method === "PATCH" || method === "PUT" || method === "DELETE") {
      opts.body = new FormData(form);
    } else if (method === "GET") {
      var params = new URLSearchParams(new FormData(form)).toString();
      if (params) url += (url.indexOf("?") === -1 ? "?" : "&") + params;
    }

    fetch(url, opts)
      .then(function (resp) {
        if (!resp.ok) {
          return resp.text().then(function (text) {
            var errMsg = "HTTP " + resp.status;
            try {
              var json = JSON.parse(text);
              errMsg = json.detail || json.error || json.message || errMsg;
            } catch (e) {
              if (text) errMsg += ": " + text.substring(0, 200);
            }
            throw new Error(errMsg);
          });
        }
        return resp.json();
      })
      .then(function (data) {
        if (renderName && renderers[renderName]) {
          renderers[renderName](target, data);
        } else {
          renderSuccess(target, data);
        }
        if (reload) {
          setTimeout(function () { location.reload(); }, 1200);
        }
      })
      .catch(function (err) {
        renderError(target, err.message || "Request failed");
      })
      .finally(function () {
        btns.forEach(function (b) { b.disabled = false; });
      });
  }

  // -----------------------------------------------------------------------
  // Core: document-level click handler for standalone buttons
  // -----------------------------------------------------------------------

  function handleButtonClick(ev) {
    var btn = ev.target.closest("button[data-api]");
    if (!btn) return;
    // If this button is inside a form with data-api, the form submit handler will handle it
    var parentForm = btn.closest("form[data-api]");
    if (parentForm) return;

    ev.preventDefault();
    ev.stopPropagation();

    var url = btn.getAttribute("data-api");
    var method = (btn.getAttribute("data-method") || "POST").toUpperCase();
    var target = findResultTarget(btn);
    var renderName = btn.getAttribute("data-render");

    renderLoading(target);
    btn.disabled = true;

    fetch(url, { method: method })
      .then(function (resp) {
        if (!resp.ok) {
          return resp.text().then(function (text) {
            var errMsg = "HTTP " + resp.status;
            try {
              var json = JSON.parse(text);
              errMsg = json.detail || json.error || json.message || errMsg;
            } catch (e) {
              if (text) errMsg += ": " + text.substring(0, 200);
            }
            throw new Error(errMsg);
          });
        }
        return resp.json();
      })
      .then(function (data) {
        if (renderName && renderers[renderName]) {
          renderers[renderName](target, data);
        } else {
          renderSuccess(target, data);
        }
      })
      .catch(function (err) {
        renderError(target, err.message || "Request failed");
      })
      .finally(function () {
        btn.disabled = false;
      });
  }

  // -----------------------------------------------------------------------
  // Programmatic API call (for use from inline scripts)
  // -----------------------------------------------------------------------

  window.runApiAction = function (endpoint, payload, targetSelector) {
    var target = qs(targetSelector);
    if (!target) {
      target = ce("div", { "class": "api-result" });
      document.body.appendChild(target);
    }
    renderLoading(target);
    var opts = { method: "POST" };
    if (payload) {
      var fd = new FormData();
      Object.keys(payload).forEach(function (k) { fd.append(k, payload[k]); });
      opts.body = fd;
    }
    return fetch(endpoint, opts)
      .then(function (r) { return r.json(); })
      .then(function (data) { renderSuccess(target, data); return data; })
      .catch(function (err) { renderError(target, err.message); });
  };

  // -----------------------------------------------------------------------
  // Attach ONCE at document level — catches ALL forms and buttons
  // -----------------------------------------------------------------------

  document.addEventListener("submit", handleFormSubmit, true);
  document.addEventListener("click", handleButtonClick, false);

  // Debug: log that app.js loaded successfully
  console.log("[RogueLink] app.js loaded — API form handler active");

})();
