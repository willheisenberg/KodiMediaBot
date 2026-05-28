package telegram

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"
)

// ValidateWebappInitData validates Telegram Mini App initData using HMAC-SHA256.
func ValidateWebappInitData(initData, botToken string, maxAgeS int) bool {
	if initData == "" || botToken == "" {
		return false
	}
	values, err := url.ParseQuery(initData)
	if err != nil {
		return false
	}
	hash := values.Get("hash")
	if hash == "" {
		return false
	}

	// Build data-check-string (sorted key=value pairs, excluding "hash")
	var pairs []string
	for key, vals := range values {
		if key == "hash" {
			continue
		}
		for _, v := range vals {
			pairs = append(pairs, key+"="+v)
		}
	}
	sort.Strings(pairs)
	dataCheckString := strings.Join(pairs, "\n")

	// HMAC-SHA256: secret_key = HMAC_SHA256(bot_token, "WebAppData")
	secretKeyMAC := hmac.New(sha256.New, []byte("WebAppData"))
	secretKeyMAC.Write([]byte(botToken))
	secretKey := secretKeyMAC.Sum(nil)

	// HMAC-SHA256: hash = HMAC_SHA256(data_check_string, secret_key)
	dataMAC := hmac.New(sha256.New, secretKey)
	dataMAC.Write([]byte(dataCheckString))
	computedHash := hex.EncodeToString(dataMAC.Sum(nil))

	if computedHash != hash {
		return false
	}

	// Check auth_date for freshness
	authDateStr := values.Get("auth_date")
	if maxAgeS > 0 && authDateStr != "" {
		authDate, err := strconv.ParseInt(authDateStr, 10, 64)
		if err == nil {
			age := time.Now().Unix() - authDate
			if age > int64(maxAgeS) {
				return false
			}
		}
	}
	return true
}

// BuildHAColorWebappHTML generates the full Mini App HTML for HA light control.
func BuildHAColorWebappHTML(appBaseURL string) string {
	return fmt.Sprintf(`<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Light Color</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:var(--tg-theme-bg-color,#1e1e2e);color:var(--tg-theme-text-color,#cdd6f4);
padding:16px;min-height:100vh}
.card{background:var(--tg-theme-secondary-bg-color,#313244);
border-radius:12px;padding:16px;margin-bottom:12px}
.card h3{font-size:14px;margin-bottom:8px;opacity:.7}
#colorPreview{width:100%%;height:80px;border-radius:8px;margin-bottom:12px}
input[type=range]{width:100%%;accent-color:var(--tg-theme-button-color,#89b4fa)}
.slider-row{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.slider-row label{min-width:20px;font-weight:600;font-size:14px}
.slider-row span{min-width:30px;text-align:right;font-size:13px;opacity:.8}
.btn{display:inline-flex;align-items:center;justify-content:center;
padding:8px 16px;border:none;border-radius:8px;cursor:pointer;font-size:14px;
background:var(--tg-theme-button-color,#89b4fa);
color:var(--tg-theme-button-text-color,#1e1e2e);font-weight:600;margin:4px}
.btn:active{opacity:.8}
.btn-sm{padding:6px 10px;font-size:12px}
.presets{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.preset{width:36px;height:36px;border-radius:50%%;border:2px solid transparent;cursor:pointer}
.preset:hover{border-color:var(--tg-theme-button-color,#89b4fa)}
.preset.active{border-color:#fff}
.actions{display:flex;flex-wrap:wrap;gap:6px;justify-content:center;margin-top:12px}
#status{text-align:center;font-size:12px;opacity:.6;margin-top:8px}
</style>
</head>
<body>
<div class="card">
<h3>Vorschau</h3>
<div id="colorPreview"></div>
</div>
<div class="card">
<h3>Farbe</h3>
<div class="slider-row"><label>R</label><input type="range" id="rSlider" min="0" max="255" value="255"/><span id="rVal">255</span></div>
<div class="slider-row"><label>G</label><input type="range" id="gSlider" min="0" max="255" value="255"/><span id="gVal">255</span></div>
<div class="slider-row"><label>B</label><input type="range" id="bSlider" min="0" max="255" value="255"/><span id="bVal">255</span></div>
</div>
<div class="card">
<h3>Helligkeit</h3>
<div class="slider-row"><label>%%</label><input type="range" id="brightSlider" min="0" max="100" value="100"/><span id="brightVal">100</span></div>
</div>
<div class="card">
<h3>Gespeicherte Farben</h3>
<div class="presets" id="presets"></div>
</div>
<div class="actions">
<button class="btn" id="applyBtn">💡 Anwenden</button>
<button class="btn" id="saveBtn">💾 Speichern</button>
</div>
<div id="status"></div>
<script>
const BASE=%q;
const tg=window.Telegram.WebApp;
tg.ready();
tg.expand();
const initData=tg.initData;

const rS=document.getElementById("rSlider"),gS=document.getElementById("gSlider"),bS=document.getElementById("bSlider"),brS=document.getElementById("brightSlider");
const rV=document.getElementById("rVal"),gV=document.getElementById("gVal"),bV=document.getElementById("bVal"),brV=document.getElementById("brightVal");
const preview=document.getElementById("colorPreview"),presetsDiv=document.getElementById("presets"),status=document.getElementById("status");

function updatePreview(){
  const r=rS.value,g=gS.value,b=bS.value;
  rV.textContent=r;gV.textContent=g;bV.textContent=b;brV.textContent=brS.value;
  preview.style.background="rgb("+r+","+g+","+b+")";
}
[rS,gS,bS,brS].forEach(s=>s.addEventListener("input",updatePreview));
updatePreview();

async function apiCall(endpoint,body){
  body.init_data=initData;
  const r=await fetch(BASE+"/"+endpoint,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  return r.json();
}

async function loadState(){
  try{
    const d=await apiCall("state",{});
    if(!d.ok)return;
    const s=d.light_state||{};
    if(s.rgb_color){const[r,g,b]=s.rgb_color;rS.value=r;gS.value=g;bS.value=b;}
    if(s.brightness!=null){const pct=Math.round(s.brightness/255*100);brS.value=pct;}
    renderPresets(d.saved_colors||[]);
    updatePreview();
  }catch(e){status.textContent="Fehler beim Laden";}
}

function renderPresets(colors){
  presetsDiv.innerHTML="";
  colors.forEach(c=>{
    const el=document.createElement("div");
    el.className="preset";
    el.style.background="rgb("+c.r+","+c.g+","+c.b+")";
    el.title=c.name;
    el.onclick=()=>{rS.value=c.r;gS.value=c.g;bS.value=c.b;updatePreview();};
    presetsDiv.appendChild(el);
  });
}

document.getElementById("applyBtn").onclick=async()=>{
  status.textContent="Wird angewendet...";
  try{
    const d=await apiCall("apply",{r:+rS.value,g:+gS.value,b:+bS.value,brightness_pct:+brS.value});
    status.textContent=d.ok?"✅ Angewendet":"❌ "+d.error;
    if(d.saved_colors)renderPresets(d.saved_colors);
  }catch(e){status.textContent="❌ Netzwerkfehler";}
};

document.getElementById("saveBtn").onclick=async()=>{
  const name=prompt("Name für die Farbe:");
  if(!name)return;
  status.textContent="Wird gespeichert...";
  try{
    const d=await apiCall("save",{r:+rS.value,g:+gS.value,b:+bS.value,name});
    status.textContent=d.ok?"✅ Gespeichert":"❌ "+d.error;
    if(d.saved_colors)renderPresets(d.saved_colors);
  }catch(e){status.textContent="❌ Netzwerkfehler";}
};

loadState();
</script>
</body>
</html>`, appBaseURL)
}
