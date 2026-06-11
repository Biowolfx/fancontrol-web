console.log("=== FanControl Web v2.9 - main.js LOADED ===");

var chart = null;
var allSensors = [];
var fanConfigs = {};
var currentData = null;
var fansBuilt = false;
var buildingConfig = false;
var activeSliders = new Set();

// Function for safe DOM element IDs
function safeId(key) {
    return key.replace(/[^a-zA-Z0-9]/g, '_');
}

var lastValidTemp = 30;
var wizardStep = 'intro';

console.log("=== Creating socket connection ===");
var socket = io();
console.log("=== Socket created ===");

socket.on("connect", function() {
    console.log("=== Socket CONNECTED ===");
});

socket.on("update", function(d) {
    console.log("=== Update received ===", d);
    updateValues(d);
});

socket.on("hardware_discovered", function(data) {
    console.log("=== Hardware discovered event ===", data);
    if (wizardStep === 'intro' && data) {
        renderDiscoveredHardware(data);
        wizardStep = 'results';
    }
});

socket.on("test_progress", function(p) {
    console.log("=== Test progress ===", p);
    
    if (wizardStep !== 'calibrating' && p.step > 0) {
        wizardStep = 'calibrating';
        var introScreen = document.getElementById("setup-step-intro");
        var resultsScreen = document.getElementById("setup-step-results");
        var actionBlock = document.getElementById("setup-step-action");
        
        if (introScreen) introScreen.style.display = "none";
        if (resultsScreen) resultsScreen.style.display = "block";
        if (actionBlock) actionBlock.style.display = "block";
        
        var btn = document.getElementById("calibrate-btn");
        var loader = document.getElementById("calibrate-loader");
        if (btn) btn.disabled = true;
        if (loader) loader.style.display = "block";
    }
    
    var loader = document.getElementById("calibrate-loader");
    if (loader && p.status) {
        loader.textContent = p.status + " (" + p.step + "/" + p.total + ")";
    }
    
    var tp = document.getElementById("test-progress");
    if (tp) tp.style.display = "block";
    var ts = document.getElementById("test-status");
    if (ts) ts.textContent = p.status + " (" + p.step + "/" + p.total + ")";
});

socket.on("test_complete", function(data) {
    console.log("=== Test complete ===", data);
    var tp = document.getElementById("test-progress");
    if (tp) tp.style.display = "none";

    if (data && data.success && data.initialized) {
        wizardStep = 'done';
        alert("🎉 Калибровка успешно завершена!\nВсе кривые PWM/RPM построены. Переходим на главный экран управления.");
        fansBuilt = false;
    } else if (!data || !data.success) {
        alert("Calibration completed with errors! Check server logs.");
        
        wizardStep = 'results';
        var intro = document.getElementById("setup-step-intro");
        var results = document.getElementById("setup-step-results");
        var action = document.getElementById("setup-step-action");
        
        if (intro) intro.style.display = "none";
        if (results) results.style.display = "block";
        if (action) action.style.display = "block";
        
        var btn = document.getElementById("calibrate-btn");
        var loader = document.getElementById("calibrate-loader");
        if (btn) btn.disabled = false;
        if (loader) loader.style.display = "none";
    }
});

// ====================== WIZARD FUNCTIONS ======================

function runDiscovery() {
    console.log("=== runDiscovery: Phase 1 - Hardware Scan ===");
    
    var btn = document.getElementById("discover-btn");
    var loader = document.getElementById("discover-loader");
    if (btn) btn.disabled = true;
    if (loader) loader.style.display = "block";
    
    wizardStep = 'scanning';
    
    fetch("/api/discover", { method: "POST" })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            console.log("=== Discovery result ===", data);
            
            if (btn) btn.disabled = false;
            if (loader) loader.style.display = "none";
            
            if (data.status === "ok") {
                renderDiscoveredHardware(data);
                wizardStep = 'results';
                
                var intro = document.getElementById("setup-step-intro");
                var results = document.getElementById("setup-step-results");
                if (intro) intro.style.display = "none";
                if (results) results.style.display = "block";
            } else {
                alert("Scan error: " + data.message);
                wizardStep = 'intro';
            }
        })
        .catch(function(err) {
            console.error("=== Discovery error ===", err);
            alert("Connection error during scan");
            if (btn) btn.disabled = false;
            if (loader) loader.style.display = "none";
            wizardStep = 'intro';
        });
}

function renderDiscoveredHardware(data) {
    var container = document.getElementById("discovered-devices");
    if (!container) return;
    
    var fanSvg = '<svg class="fan-icon-svg" viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">' +
        '<defs>' +
            '<linearGradient id="bladeGrad" x1="0%" y1="0%" x2="100%" y2="100%">' +
                '<stop offset="0%" style="stop-color:#4a90d9;stop-opacity:1" />' +
                '<stop offset="100%" style="stop-color:#00ff88;stop-opacity:1" />' +
            '</linearGradient>' +
        '</defs>' +
        '<circle cx="50" cy="50" r="12" fill="#666" stroke="#4a90d9" stroke-width="2"/>' +
        '<circle cx="50" cy="50" r="6" fill="#333"/>' +
        '<g fill="url(#bladeGrad)" opacity="0.9">' +
            '<path d="M50,50 L50,8 Q55,15 60,10 Q58,25 65,20 L50,50Z" />' +
            '<path d="M50,50 L90,50 Q82,55 86,60 Q72,58 76,65 L50,50Z" />' +
            '<path d="M50,50 L50,92 Q45,85 40,90 Q42,75 35,80 L50,50Z" />' +
            '<path d="M50,50 L10,50 Q18,45 14,40 Q28,42 24,35 L50,50Z" />' +
        '</g>' +
    '</svg>';

    var html = '<div class="wizard-layout">';
    
    // � �� ›� ћ� љ 1: � ў� •� њ� џ� •� � � ђ� ў� Ј� � � ќ� «� • � ”� ђ� ў� §� �� љ� � � � � ”� �� Ў� љ� �
    html += '<div class="wizard-block">';
    html += '<h5>рџЊЎпёЏ Sensors & Drives</h5>';
    html += '<div id="wizard-sensors-list">';
    
    // � ’С‹� І� ѕ� ґ � ґ� ёСЃ� є� ѕ� І
    if (data.disks && Object.keys(data.disks).length > 0) {
        for (var k in data.disks) {
            var d = data.disks[k];
            var diskId = safeId(k);
            html += '<div class="discovered-device" id="wdrive-' + diskId + '">' +
                    '<span>рџ’ѕ ' + d.label + ' <small class="text-muted">(' + d.type.toUpperCase() + ')</small></span>' +
                    '<span class="drive-temp-live" style="font-weight:bold; color:#ffaa00;">' + (d.standby ? 'Sleep' : (d.temp > 0 ? d.temp + 'В°C' : '--')) + '</span>' +
                    '</div>';
        }
    }
    
    // � ’С‹� І� ѕ� ґ СЃ� µ� ЅСЃ� ѕСЂ� ѕ� І � �� °С‚� µСЂ� ё� ЅСЃ� є� ѕ� № � ї� »� °С‚С‹ / CPU
    if (data.temps && Object.keys(data.temps).length > 0) {
        for (var tk in data.temps) {
            var t = data.temps[tk];
            var tempId = safeId(tk);
            html += '<div class="discovered-device" id="wtemp-' + tempId + '">' +
                    '<span>рџЊї ' + t.label + '</span>' +
                    '<span class="sensor-temp-live" style="font-weight:bold; color:#ffaa00;">' + (t.value || 0) + 'В°C</span>' +
                    '</div>';
        }
    }
    html += '</div></div>';
    
    // � �� ›� ћ� љ 2: � ’� •� ќ� ў� �� ›� Ї� ў� ћ� � � «
    html += '<div class="wizard-block">';
    html += '<h5>рџЊЂ Fans</h5>';
    html += '<div id="wizard-fans-list">';
    
    if (data.fans && Object.keys(data.fans).length > 0) {
        for (var key in data.fans) {
            var fan = data.fans[key];
            var fanId = safeId(key);
            
            html += '<div class="discovered-device" id="device-' + fanId + '" style="display:flex;align-items:center;justify-content:space-between">' +
                    '<div style="display:flex;align-items:center;gap:10px;flex:1">' +
                    '<span id="icon-' + fanId + '" style="display:flex;align-items:center">' + fanSvg + '</span>' +
                    '<div>' +
                    '<div style="font-weight:bold;font-size:14px">' + fan.label + '</div>' +
                    '<div style="font-size:10px;color:#888">' + key + ' | ' + (fan.writable ? 'вњ… Controllable' : 'вљ� пёЏ Read-only') + '</div>' +
                    '</div>' +
                    '</div>' +
                    '<div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px;">' +
                    '<span class="fan-status-live"><span class="badge-need-calib">Not calibrated</span></span>' +
                    '<span class="fan-rpm-live" style="font-weight:bold;color:#00ff88;font-size:14px;">0 RPM</span>' +
                    '</div>' +
                    '</div>';
        }
    }
    html += '</div></div>';
    html += '</div>'; // � љ� ѕ� Ѕ� µС�  wizard-layout
    
    container.innerHTML = html;
    
    var intro = document.getElementById("setup-step-intro");
    var results = document.getElementById("setup-step-results");
    var action = document.getElementById("setup-step-action");
    
    if (intro) intro.style.display = "none";
    if (results) results.style.display = "block";
    
    if (data.fans && Object.keys(data.fans).length > 0 && action) {
        action.style.display = "block";
    }
}

function runCalibration() {
    console.log("=== runCalibration: Phase 2 - Fan Calibration ===");
    
    var btn = document.getElementById("calibrate-btn");
    var loader = document.getElementById("calibrate-loader");
    if (btn) btn.disabled = true;
    if (loader) loader.style.display = "block";
    
    wizardStep = 'calibrating';
    
    fetch("/api/initialize", { method: "POST" })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            console.log("=== Calibration started ===", data);
        })
        .catch(function(err) {
            console.error("=== Calibration error ===", err);
            alert("Calibration launch error");
            if (btn) btn.disabled = false;
            if (loader) loader.style.display = "none";
            wizardStep = 'results';
        });
}

// ====================== MAIN SCREEN FUNCTIONS ======================

function showSyncingStatus() {
    var statusEl = document.getElementById("sync-status");
    if (statusEl) {
        statusEl.textContent = "вџі Saving...";
        statusEl.className = "saving";
        clearTimeout(window._syncTimeout);
        window._syncTimeout = setTimeout(function() {
            if (statusEl) {
                statusEl.textContent = "в—Џ Synced";
                statusEl.className = "synced";
            }
        }, 3000);
    }
}

function updateValues(d) {
    currentData = d;
    
    // � •СЃ� »� ё � �С‹ � Ѕ� ° СЌ� єСЂ� °� Ѕ� µ � �� °СЃС‚� µСЂ� ° � Ѕ� °СЃС‚СЂ� ѕ� №� є� ё (� є� °� »� ё� ±СЂ� ѕ� І� є� ё)
    if (wizardStep === 'results' || wizardStep === 'calibrating') {
        
        // 1. � ћ� ±� Ѕ� ѕ� І� »� µ� Ѕ� ё� µ С‚� µ� �� ї� µСЂ� °С‚СѓСЂ � ґ� ёСЃ� є� ѕ� І
        if (d.hdd_sensors) {
            for (var dk in d.hdd_sensors) {
                var disk = d.hdd_sensors[dk];
                var diskId = safeId(dk);
                var driveRow = document.getElementById("wdrive-" + diskId);
                if (driveRow) {
                    var dTempEl = driveRow.querySelector(".drive-temp-live");
                    if (dTempEl) {
                        dTempEl.textContent = disk.standby ? 'Sleep' : (disk.temp > 0 ? disk.temp + 'В°C' : '--');
                    }
                }
            }
        }
        
        // 2. � ћ� ±� Ѕ� ѕ� І� »� µ� Ѕ� ё� µ � ґ� °С‚С‡� ё� є� ѕ� І � �� °С‚� µСЂ� ё� ЅСЃ� є� ѕ� № � ї� »� °С‚С‹ / CPU
        if (d.temp_sensors) {
            for (var tk in d.temp_sensors) {
                var sensor = d.temp_sensors[tk];
                var tempId = safeId(tk);
                var tempRow = document.getElementById("wtemp-" + tempId);
                if (tempRow) {
                    var sTempEl = tempRow.querySelector(".sensor-temp-live");
                    if (sTempEl) {
                        sTempEl.textContent = (sensor.value || 0) + 'В°C';
                    }
                }
            }
        }
        
        // 3. � ћ� ±� Ѕ� ѕ� І� »� µ� Ѕ� ё� µ � І� µ� ЅС‚� ё� »СЏС‚� ѕСЂ� ѕ� І
        if (d.fans) {
            for (var key in d.fans) {
                var fan = d.fans[key];
                var deviceId = safeId(key);
                var deviceRow = document.getElementById("device-" + deviceId);
                
                if (deviceRow) {
                    var rpmEl = deviceRow.querySelector(".fan-rpm-live");
                    if (rpmEl) rpmEl.textContent = fan.rpm + " RPM";
                    
                    var statusEl = deviceRow.querySelector(".fan-status-live");
                    if (statusEl) {
                        if (fan.status === "calibrating") {
                            statusEl.innerHTML = '<span class="text-info calibrating-pulse">вљЎ Calibrating...</span>';
                        } else if (fan.status === "normal") {
                            statusEl.innerHTML = '<span style="color:#00ff88">вњ“ Normal</span>';
                        } else if (fan.status === "inverted") {
                            statusEl.innerHTML = '<span style="color:#ffaa00">в‡„ Inverted</span>';
                        } else if (fan.status === "not_connected") {
                            statusEl.innerHTML = '<span style="color:#ff4444">вњ— Not connected</span>';
                        }
                    }
                    
                    // � ’СЂ� °С‰� µ� Ѕ� ё� µ � ё� є� ѕ� Ѕ� є� ё
                    var iconContainer = document.getElementById("icon-" + safeId);
                    if (iconContainer) {
                        var svgElement = iconContainer.querySelector(".fan-icon-svg");
                        if (svgElement) {
                            var currentRpm = fan.rpm || 0;
                            if (currentRpm > 0) {
                                var visualDuration = (60 / currentRpm) * 10;
                                if (visualDuration < 0.3) visualDuration = 0.3;
                                if (visualDuration > 5.0) visualDuration = 5.0;
                                
                                svgElement.style.animationDuration = visualDuration.toFixed(2) + "s";
                                svgElement.classList.add("fan-spinning");
                            } else {
                                svgElement.classList.remove("fan-spinning");
                                svgElement.style.animationDuration = "0s";
                            }
                        }
                    }
                }
            }
        }
    }

    // If system is not initialized, show setup screen
    if (!d.initialized) {
        var ss = document.getElementById("setup-screen");
        var ms = document.getElementById("main-screen");
        if (ss) ss.style.display = "block";
        if (ms) ms.style.display = "none";
        
        if (d.hardware_scanned && wizardStep === 'intro') {
            renderDiscoveredHardware({
                fans: d.fans,
                temps: d.temp_sensors,
                disks: d.hdd_sensors
            });
            wizardStep = 'results';
            
            var btn = document.getElementById("discover-btn");
            var loader = document.getElementById("discover-loader");
            if (btn) btn.disabled = false;
            if (loader) loader.style.display = "none";
        }
        return;
    }

    // Show main screen
    var ss = document.getElementById("setup-screen");
    var ms = document.getElementById("main-screen");
    if (ss) ss.style.display = "none";
    if (ms) ms.style.display = "block";

    var mt = document.getElementById("max-temp-disp");
    if (mt) {
        var tc = "temp-good";
        if (d.max_hdd_temp > 35) tc = "temp-bad";
        else if (d.max_hdd_temp > 31) tc = "temp-warn";
        
        var tempSpan = mt.querySelector('span') || document.createElement('span');
        tempSpan.className = tc;
        tempSpan.textContent = d.max_hdd_temp + 'В°C';
        if (!mt.contains(tempSpan)) {
            mt.textContent = '';
            mt.appendChild(tempSpan);
        }
    }

    var tw = document.getElementById("test-warning");
    if (tw) tw.style.display = d.tested ? "none" : "inline";

    allSensors = [];
    for (var k in d.hdd_sensors) {
        var s = d.hdd_sensors[k];
        allSensors.push({id: "hdd:" + k, label: s.label, temp: s.temp, standby: s.standby, group: "Disks"});
    }
    for (k in d.temp_sensors) {
        allSensors.push({id: "temp:" + k, label: d.temp_sensors[k].label, temp: d.temp_sensors[k].value, standby: false, group: "Sensors"});
    }

    var dc = document.getElementById("disks-container");
    if (dc) {
        dc.innerHTML = '';
        for (k in d.hdd_sensors) {
            var v = d.hdd_sensors[k];
            var html = '<div style="display:flex;justify-content:space-between;padding:3px 0;font-size:13px">' +
                       '<span>' + v.label + '</span>' +
                       '<span style="color:' + (v.standby ? '#4a90d9' : v.temp > 35 ? '#ff4444' : v.temp > 31 ? '#ffaa00' : '#00ff88') + '">' +
                       (v.standby ? 'Sleep' : v.temp > 0 ? v.temp + 'В°C' : '--') + '</span></div>';
            dc.innerHTML += html;
        }
    }

    var tc2 = document.getElementById("temps-container");
    if (tc2) {
        tc2.innerHTML = '';
        for (k in d.temp_sensors) {
            var tv = d.temp_sensors[k];
            tc2.innerHTML += '<div style="display:flex;justify-content:space-between;padding:3px 0;font-size:13px">' +
                            '<span>' + tv.label + '</span>' +
                            '<span>' + (tv.value || 0) + 'В°C</span></div>';
        }
    }

    if (!fansBuilt) {
        buildFans(d);
        fansBuilt = true;
    }

    for (k in d.fans) {
        var f = d.fans[k];
            var ks = safeId(k);
        if (rpmEl) {
            var rpmText = f.rpm + " RPM";
            if (f.rpm_stabilizing) rpmText += " вЏі";
            rpmEl.textContent = rpmText;
        }

        var sliderEl = document.getElementById("slider-" + ks);
        if (sliderEl && !activeSliders.has(ks)) {
            sliderEl.disabled = (f.fan_mode === "auto");
            if (f.fan_mode !== "auto") {
                sliderEl.value = f.manual_pct || 50;
            }
        }

        var pwmEl = document.getElementById("pwm-" + ks);
        if (pwmEl && (!sliderEl || document.activeElement !== sliderEl)) {
            if (f.fan_mode === "auto") {
                pwmEl.textContent = (f.current_pct !== undefined ? f.current_pct : 50) + "%";
            } else {
                pwmEl.textContent = (f.manual_pct || 50) + "%";
            }
        }

        var stEl = document.getElementById("status-" + ks);
        if (stEl) {
            var st = f.status || "not_tested";
            var labels = {normal: "Normal", inverted: "Inverted", absent: "Absent", not_tested: "Not tested", not_connected: "Not connected"};
            var classes = {normal: "temp-good", inverted: "text-warning", absent: "text-muted", not_tested: "text-danger", not_connected: "text-muted"};
            stEl.textContent = labels[st] || st;
            stEl.className = classes[st] || "";
        }

        var card = document.getElementById("card-" + ks);
        if (card) {
            card.className = "card " + (f.fan_mode === "auto" ? "auto-mode" : "manual-mode");
        }
    }
}

function buildFans(d) {
    var fc = document.getElementById("fan-container");
    if (!fc) return;
    
    var html = "";
    for (var k in d.fans) {
        var f = d.fans[k];
        var ks = safeId(k);
        html += "<div class='card " + (f.fan_mode === "auto" ? "auto-mode" : "manual-mode") + "' id='card-" + ks + "'>";
        html += "<div class='card-header'>" + f.label + " <small id='status-" + ks + "'></small></div>";
        html += "<div class='card-body'>";
        html += "<div class='fan-row'>";
        html += "<span class='name'>" + f.label + "</span>";
        html += " <button class='sensor-btn' data-fan='" + k + "'>Test</button>";
        html += "<span class='rpm' id='rpm-" + ks + "'>0 RPM</span>";
        html += "<input type='range' min='0' max='100' value='" + (f.manual_pct || 50) + "' data-fan='" + k + "' " + (d.tested ? "" : "disabled") + " id='slider-" + ks + "'>";
        html += "<small id='pwm-" + ks + "'>" + (f.manual_pct || 50) + "%</small>";
        html += "</div><div id='config-" + ks + "' style='font-size:12px;margin-top:5px;display:flex;align-items:center;gap:5px;flex-wrap:wrap'></div>";
        html += "<div id='schedule-" + ks + "'></div>";
        html += "</div></div>";
    }
    fc.innerHTML = html;
    
    var fcd = document.getElementById("fan-count-disp");
    if (fcd) fcd.textContent = Object.keys(d.fans).length;

    var sliders = fc.querySelectorAll("input[type=range]");
    for (var i = 0; i < sliders.length; i++) {
        (function(slider) {
            var fanKey = slider.getAttribute("data-fan");
            var ks2 = safeId(fanKey);
            
            slider.addEventListener("input", function() {
                var pwmEl = document.getElementById("pwm-" + ks2);
                if (pwmEl) pwmEl.textContent = this.value + "%";
            });
            
            slider.addEventListener("change", function() {
                showSyncingStatus();
                setFan(fanKey, this.value);
            });
            
            slider.addEventListener("mousedown", function() { activeSliders.add(ks2); });
            slider.addEventListener("mouseup", function() { 
                setTimeout(function() { activeSliders.delete(ks2); }, 1500); 
            });
        })(sliders[i]);
    }

    var buttons = fc.querySelectorAll("button.sensor-btn");
    for (var j = 0; j < buttons.length; j++) {
        (function(btn) {
            btn.addEventListener("click", function() { testFan(this.getAttribute("data-fan")); });
        })(buttons[j]);
    }

    for (k in d.fans) {
        if (!fanConfigs[k]) fanConfigs[k] = {};
        var f2 = d.fans[k];
        fanConfigs[k].sensors = f2.sensors || [];
        fanConfigs[k].sensor_mode = f2.sensor_mode || "max";
        fanConfigs[k].target_temp = f2.target_temp || 31;
        fanConfigs[k].fan_mode = f2.fan_mode || "manual";
        fanConfigs[k].schedule = f2.schedule || [];
        buildFanConfig(k, d);
    }
}

function buildFanConfig(k, d) {
    buildingConfig = true;
    var f = d.fans[k];
    var cfg = fanConfigs[k] || {};
    var ks = safeId(k);
    var sensors = cfg.sensors || [];
    var smode = cfg.sensor_mode || "max";
    var target = cfg.target_temp || 31;
    var fm = cfg.fan_mode || "manual";

    var configDiv = document.getElementById("config-" + ks);
    if (!configDiv) { buildingConfig = false; return; }

    var addBtn = document.createElement("button");
    addBtn.className = "sensor-btn";
    addBtn.textContent = "+";
    addBtn.onclick = function(e) { e.stopPropagation(); togglePopup(k, this); };

    var tagsDiv = document.createElement("div");
    tagsDiv.style.cssText = "display:flex;flex-wrap:wrap;gap:2px";
    for (var i = 0; i < sensors.length; i++) {
        var s = sensors[i];
        var found = allSensors.find(function(y) { return y.id === s; });
        var tag = document.createElement("span");
        tag.className = "sensor-tag";
        tag.setAttribute("data-sid", s);
        tag.textContent = (found ? found.label : s) + " ";
        var rm = document.createElement("span");
        rm.className = "remove";
        rm.textContent = "x";
        rm.onclick = function(e) {
            e.stopPropagation();
            removeSensor(k, this.parentNode.getAttribute("data-sid"));
        };
        tag.appendChild(rm);
        tagsDiv.appendChild(tag);
    }

    var smodeSel = document.createElement("select");
    smodeSel.innerHTML = "<option value='max'>Max</option><option value='min'>Min</option><option value='avg'>Avg</option>";
    smodeSel.value = smode;
    smodeSel.onchange = function() { showSyncingStatus(); setFanConfig(k, "sensor_mode", this.value); };

    var targetInput = document.createElement("input");
    targetInput.type = "number"; targetInput.value = target; targetInput.min = 20; targetInput.max = 60;
    targetInput.style.width = "45px";
    targetInput.onchange = function() { showSyncingStatus(); setFanConfig(k, "target_temp", this.value); };

    var fmSel = document.createElement("select");
    fmSel.innerHTML = "<option value='manual'>Manual</option><option value='auto'>Auto</option>";
    fmSel.value = fm;
    fmSel.onchange = function() { showSyncingStatus(); setFanConfig(k, "fan_mode", this.value); };

    configDiv.innerHTML = "";
    configDiv.appendChild(addBtn);
    configDiv.appendChild(tagsDiv);
    configDiv.appendChild(smodeSel);
    configDiv.appendChild(document.createTextNode(" Target:"));
    configDiv.appendChild(targetInput);
    configDiv.appendChild(document.createTextNode("В°C "));
    configDiv.appendChild(fmSel);

    buildSchedule(k, ks, fm, cfg.schedule || []);
    setTimeout(function() { buildingConfig = false; }, 100);
}

function buildSchedule(k, ks, fm, schedule) {
    var schedDiv = document.getElementById("schedule-" + ks);
    if (!schedDiv) return;
    schedDiv.innerHTML = "";
    if (fm !== "auto") return;

    var builder = document.createElement("div");
    builder.className = "timeline-builder";
    var header = document.createElement("div");
    header.style.cssText = "display:flex;justify-content:space-between;align-items:center;margin-bottom:8px";
    header.innerHTML = "<span style='color:#aaa;font-size:12px'>Flexible Schedule</span>";
    var addBtn = document.createElement("button");
    addBtn.textContent = "+ Add";
    addBtn.style.cssText = "font-size:11px;padding:2px 8px;background:#00aa00;color:#fff;border:none;border-radius:3px";
    addBtn.onclick = function() { addSchedule(k); };
    header.appendChild(addBtn);
    builder.appendChild(header);

    if (schedule.length === 0) {
        var empty = document.createElement("div");
        empty.style.cssText = "text-align:center;color:#888;font-size:11px;padding:10px";
        empty.textContent = "No schedule configured.";
        builder.appendChild(empty);
    }

    var days = ["mon","tue","wed","thu","fri","sat","sun"];
    var dayNames = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];
    for (var i = 0; i < schedule.length; i++) {
        var sch = schedule[i];
        var row = document.createElement("div");
        row.className = "timeline-rule-row";
        row.style.cssText = "display:flex;align-items:center;gap:8px;background:rgba(0,0,0,.2);padding:6px;border-radius:4px;margin:4px 0;flex-wrap:wrap";
        
        var daySel = document.createElement("select");
        daySel.style.cssText = "background:#1a1a2e;color:#fff;border:1px solid #333;font-size:11px";
        daySel.innerHTML = "<option value='all'>Every day</option><option value='weekday'>Weekdays</option><option value='weekend'>Weekend</option>";
        for (var di = 0; di < 7; di++) daySel.innerHTML += "<option value='" + days[di] + "'>" + dayNames[di] + "</option>";
        daySel.value = sch.day || "all";
        daySel.onchange = function() { showSyncingStatus(); updateSchedule(k, i, "day", this.value); };
        
        var timeStart = document.createElement("input"); 
        timeStart.type = "time"; 
        timeStart.value = sch.time_start || "00:00";
        timeStart.style.cssText = "background:#1a1a2e;color:#fff;border:1px solid #333;font-size:11px";
        timeStart.onchange = function() { showSyncingStatus(); updateSchedule(k, i, "time_start", this.value); };
        
        var timeEnd = document.createElement("input"); 
        timeEnd.type = "time"; 
        timeEnd.value = sch.time_end || "23:59";
        timeEnd.style.cssText = "background:#1a1a2e;color:#fff;border:1px solid #333;font-size:11px";
        timeEnd.onchange = function() { showSyncingStatus(); updateSchedule(k, i, "time_end", this.value); };
        
        var modeSel = document.createElement("select");
        modeSel.style.cssText = "background:#1a1a2e;color:#fff;border:1px solid #333;font-size:11px";
        modeSel.innerHTML = "<option value='auto'>Auto</option><option value='fixed'>Fixed</option><option value='low'>Quiet</option><option value='off'>Off</option>";
        modeSel.value = sch.mode || "auto";
        modeSel.onchange = function() {
            sch.mode = this.value;
            showSyncingStatus();
            updateSchedule(k, i, "mode", sch.mode);
            rebuildFanConfig(k);
        };
        
        var delBtn = document.createElement("button");
        delBtn.textContent = "x";
        delBtn.style.cssText = "color:#ff4444;background:none;border:none;font-size:16px;cursor:pointer;margin-left:auto";
        delBtn.onclick = function() { removeSchedule(k, i); };
        
        row.appendChild(daySel);
        row.appendChild(document.createTextNode(" from ")); 
        row.appendChild(timeStart);
        row.appendChild(document.createTextNode(" to ")); 
        row.appendChild(timeEnd);
        row.appendChild(modeSel);
        if (sch.mode === "auto" || !sch.mode) {
            row.appendChild(document.createTextNode(" Цель:"));
            var ti = document.createElement("input");
            ti.type = "number";
            ti.value = sch.target_temp || 31;
            ti.style.cssText = "width:45px;background:#1a1a2e;color:#fff;border:1px solid #333;font-size:11px";
            ti.min = 20; ti.max = 60;
            ti.onchange = function() { showSyncingStatus(); updateSchedule(k, i, "target_temp", parseInt(this.value)); };
            row.appendChild(ti);
            row.appendChild(document.createTextNode("°C"));
        } else if (sch.mode === "fixed" || sch.mode === "low") {
            row.appendChild(document.createTextNode(" Скорость:"));

            var pctInput = document.createElement("input");
            pctInput.type = "number";
            pctInput.value = sch.speed_pct !== undefined ? sch.speed_pct : (sch.mode === "low" ? 25 : 50);
            pctInput.style.cssText = "width:45px;background:#1a1a2e;color:#fff;border:1px solid #333;font-size:11px";
            pctInput.min = 0; pctInput.max = 100;

            var pctSlider = document.createElement("input");
            pctSlider.type = "range";
            pctSlider.value = pctInput.value;
            pctSlider.min = 0; pctSlider.max = 100;
            pctSlider.style.cssText = "width:80px; margin: 0 5px; vertical-align: middle;";

            pctSlider.oninput = function() { pctInput.value = this.value; };
            pctSlider.onchange = function() { showSyncingStatus(); updateSchedule(k, i, "speed_pct", parseInt(this.value)); };
            pctInput.onchange = function() {
                if (this.value < 0) this.value = 0;
                if (this.value > 100) this.value = 100;
                pctSlider.value = this.value;
                showSyncingStatus();
                updateSchedule(k, i, "speed_pct", parseInt(this.value));
            };

            row.appendChild(pctSlider);
            row.appendChild(pctInput);
            row.appendChild(document.createTextNode("%"));
        }
        row.appendChild(delBtn);
        builder.appendChild(row);
    }
    schedDiv.appendChild(builder);
}

function togglePopup(key, btn) {
    var popup = document.getElementById("sensor-popup");
    if (!popup) return;
    var rect = btn.getBoundingClientRect();
    popup.style.left = rect.left + "px";
    popup.style.top = (rect.bottom + 4) + "px";
    
    var groups = {};
    allSensors.forEach(function(s) { 
        if (!groups[s.group]) groups[s.group] = []; 
        groups[s.group].push(s); 
    });
    
    var sensors = (fanConfigs[key] || {}).sensors || [];
    popup.innerHTML = "";
    
    for (var g in groups) {
        var gTitle = document.createElement("div");
        gTitle.style.cssText = "font-weight:bold;padding:3px 0;color:#aaa;font-size:11px";
        gTitle.textContent = g;
        popup.appendChild(gTitle);
        
        for (var i = 0; i < groups[g].length; i++) {
            var s = groups[g][i];
            var label = document.createElement("label");
            label.style.cssText = "display:flex;align-items:center;gap:5px;padding:2px 5px;cursor:pointer;font-size:12px";
            var cb = document.createElement("input");
            cb.type = "checkbox"; 
            cb.value = s.id; 
            cb.checked = sensors.includes(s.id);
            cb.onchange = function() { showSyncingStatus(); toggleSensor(key, this); };
            label.appendChild(cb);
            label.appendChild(document.createTextNode(s.label + " (" + (s.standby ? "Sleep" : s.temp + "В°C") + ")"));
            popup.appendChild(label);
        }
    }
    popup.classList.add("show");
}

function toggleSensor(key, cb) {
    var popup = document.getElementById("sensor-popup");
    if (!popup) return;
    var checks = popup.querySelectorAll("input[type=checkbox]:checked");
    var sensors = [];
    for (var i = 0; i < checks.length; i++) sensors.push(checks[i].value);
    if (sensors.length === 0) { cb.checked = true; sensors = [cb.value]; }
    if (!fanConfigs[key]) fanConfigs[key] = {};
    fanConfigs[key].sensors = sensors;
    setFanConfig(key, "sensors", sensors);
    rebuildFanConfig(key);
}

function removeSensor(key, id) {
    var cfg = fanConfigs[key] || {};
    var sensors = (cfg.sensors || []).filter(function(s) { return s !== id; });
    cfg.sensors = sensors;
    fanConfigs[key] = cfg;
    showSyncingStatus();
    setFanConfig(key, "sensors", sensors);
    rebuildFanConfig(key);
}

function addSchedule(key) {
    var cfg = fanConfigs[key] || {}, s = cfg.schedule || [];
    s.push({day: "all", time_start: "00:00", time_end: "23:59", mode: "auto", target_temp: 31});
    cfg.schedule = s; 
    fanConfigs[key] = cfg; 
    showSyncingStatus();
    setFanConfig(key, "schedule", s);
    rebuildFanConfig(key);
}

function removeSchedule(key, i) {
    var cfg = fanConfigs[key] || {}, s = cfg.schedule || [];
    s.splice(i, 1); 
    cfg.schedule = s; 
    fanConfigs[key] = cfg; 
    showSyncingStatus();
    setFanConfig(key, "schedule", s);
    rebuildFanConfig(key);
}

function updateSchedule(key, i, field, val) {
    var cfg = fanConfigs[key] || {}, s = cfg.schedule || [];
    if (!s[i]) return;
    s[i][field] = val; 
    cfg.schedule = s; 
    fanConfigs[key] = cfg; 
    setFanConfig(key, "schedule", s);
}

function rebuildFanConfig(k) { 
    if (currentData) buildFanConfig(k, currentData); 
}

function testFan(key) {
    fetch("/api/test/start", {
        method: "POST", 
        headers: {"Content-Type": "application/json"}, 
        body: JSON.stringify({fan: key})
    });
    var tp = document.getElementById("test-progress"); 
    if (tp) tp.style.display = "block";
}

function startTest() {
    fetch("/api/test/start", {method: "POST"});
    var tp = document.getElementById("test-progress"); 
    if (tp) tp.style.display = "block";
}

function setFan(k, v) {
    var ks = safeId(k);
    
    var sliderEl = document.getElementById("slider-" + ks);
    var pwmEl = document.getElementById("pwm-" + ks);
    if (pwmEl) pwmEl.textContent = v + "%";
    
    fetch("/api/control", {
        method: "POST", 
        headers: {"Content-Type": "application/json"}, 
        body: JSON.stringify({action: "set_fan_pwm", fan: k, pwm: parseInt(v)})
    })
    .then(function() {
        setTimeout(function() {
            socket.emit('get_state');
        }, 1500);
    })
    .catch(function(err) {
        console.error('Fan control error:', err);
        socket.emit('get_state');
    });
}

function setFanConfig(k, field, val) {
    if (buildingConfig) return;
    if (!fanConfigs[k]) fanConfigs[k] = {};
    var oldVal = fanConfigs[k][field];
    if (typeof val === "object" && val !== null) { 
        if (JSON.stringify(oldVal) === JSON.stringify(val)) return; 
    } else if (oldVal === val) return;
    
    fanConfigs[k][field] = val;
    var payload = {action: "set_fan_config", fan: k}; 
    payload[field] = val;
    fetch("/api/control", {
        method: "POST", 
        headers: {"Content-Type": "application/json"}, 
        body: JSON.stringify(payload)
    });
    if (field === "fan_mode") rebuildFanConfig(k);
}

// ====================== HISTORY CHART ======================

function loadChart() {
    var ctx = document.getElementById("chart");
    if (!ctx) return;

    fetch("/api/history?hours=24")
        .then(function(r) { return r.json(); })
        .then(function(d) {
            if (!d || d.length === 0) {
                console.log("Chart: no historical data");
                return;
            }

            var chartData = {labels: [], temps: [], rpm: []};
            d.forEach(function(x) {
                chartData.labels.push(new Date(x.ts).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}));
                if (x.max_temp > 0) lastValidTemp = x.max_temp;
                chartData.temps.push(lastValidTemp);
                chartData.rpm.push(x.rpm || 0);
            });

            if (!chart) {
                chart = new Chart(ctx, {
                    type: "line",
                    data: {
                        labels: chartData.labels,
                        datasets: [
                            {label: "Max HDD В°C", data: chartData.temps, borderColor: "#ff4444", yAxisID: "y1", tension: 0.3},
                            {label: "RPM", data: chartData.rpm, borderColor: "#00ff88", yAxisID: "y2", tension: 0.3}
                        ]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        scales: {
                            y1: {type: "linear", position: "left", title: {display: true, text: "В°C"}},
                            y2: {type: "linear", position: "right", title: {display: true, text: "RPM"}}
                        }
                    }
                });
            } else {
                chart.data.labels = chartData.labels;
                if (chart.data.datasets && chart.data.datasets[0]) 
                    chart.data.datasets[0].data = chartData.temps;
                if (chart.data.datasets && chart.data.datasets[1]) 
                    chart.data.datasets[1].data = chartData.rpm;
                chart.update();
            }
        })
        .catch(function(err) {
            console.error("Chart load error:", err);
        });
}

console.log("=== Calling loadChart ===");
loadChart();
setInterval(loadChart, 60000);

document.addEventListener('click', function(e) {
    var popup = document.getElementById('sensor-popup');
    if (popup && !popup.contains(e.target) && !e.target.classList.contains('sensor-btn')) {
        popup.classList.remove('show');
    }
});

console.log("=== FanControl Web v2.9 - main.js FULLY LOADED ===");
