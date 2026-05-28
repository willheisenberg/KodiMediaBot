package telegram

// LangEntry stores a flag emoji and friendly name.
type LangEntry struct {
	Flag string
	Name string
}

// LangMap maps ISO 639-1/2 language codes to flag emojis and friendly language names.
var LangMap = map[string]LangEntry{
	// German
	"deu":   {"🇩🇪", "Deutsch"},
	"ger":   {"🇩🇪", "Deutsch"},
	"de":    {"🇩🇪", "Deutsch"},
	"de-ch": {"🇨🇭", "Deutsch (CH)"},
	"de_ch": {"🇨🇭", "Deutsch (CH)"},
	"de-at": {"🇦🇹", "Deutsch (AT)"},
	"de_at": {"🇦🇹", "Deutsch (AT)"},

	// English
	"eng":   {"🇬🇧", "English"},
	"en":    {"🇬🇧", "English"},
	"en-us": {"🇺🇸", "English (US)"},
	"en_us": {"🇺🇸", "English (US)"},
	"en-gb": {"🇬🇧", "English (UK)"},
	"en_gb": {"🇬🇧", "English (UK)"},

	// French
	"fra": {"🇫🇷", "Français"},
	"fre": {"🇫🇷", "Français"},
	"fr":  {"🇫🇷", "Français"},

	// Spanish
	"spa": {"🇪🇸", "Español"},
	"es":  {"🇪🇸", "Español"},

	// Italian
	"ita": {"🇮🇹", "Italiano"},
	"it":  {"🇮🇹", "Italiano"},

	// Portuguese
	"por":   {"🇵🇹", "Português"},
	"pt":    {"🇵🇹", "Português"},
	"pt-br": {"🇧🇷", "Português (BR)"},
	"pt_br": {"🇧🇷", "Português (BR)"},

	// Dutch
	"nld": {"🇳🇱", "Nederlands"},
	"dut": {"🇳🇱", "Nederlands"},
	"nl":  {"🇳🇱", "Nederlands"},

	// Russian
	"rus": {"🇷🇺", "Русский"},
	"ru":  {"🇷🇺", "Русский"},

	// Ukrainian
	"ukr": {"🇺🇦", "Ukrainian"},
	"uk":  {"🇺🇦", "Ukrainian"},

	// Polish
	"pol": {"🇵🇱", "Polski"},
	"pl":  {"🇵🇱", "Polski"},

	// Swedish
	"swe": {"🇸🇪", "Svenska"},
	"sv":  {"🇸🇪", "Svenska"},

	// Norwegian
	"nor": {"🇳🇴", "Norsk"},
	"no":  {"🇳🇴", "Norsk"},

	// Danish
	"dan": {"🇩🇰", "Dansk"},
	"da":  {"🇩🇰", "Dansk"},

	// Finnish
	"fin": {"🇫🇮", "Suomi"},
	"fi":  {"🇫🇮", "Suomi"},

	// Turkish
	"tur": {"🇹🇷", "Türkçe"},
	"tr":  {"🇹🇷", "Türkçe"},

	// Greek
	"ell": {"🇬🇷", "Greek"},
	"gre": {"🇬🇷", "Greek"},
	"el":  {"🇬🇷", "Greek"},

	// Czech
	"ces": {"🇨🇿", "Czech"},
	"cze": {"🇨🇿", "Czech"},
	"cs":  {"🇨🇿", "Czech"},

	// Slovak
	"slk": {"🇸🇰", "Slovak"},
	"slo": {"🇸🇰", "Slovak"},
	"sk":  {"🇸🇰", "Slovak"},

	// Hungarian
	"hun": {"🇭🇺", "Hungarian"},
	"hu":  {"🇭🇺", "Hungarian"},

	// Romanian
	"ron": {"🇷🇴", "Romanian"},
	"rum": {"🇷🇴", "Romanian"},
	"ro":  {"🇷🇴", "Romanian"},

	// Bulgarian
	"bul": {"🇧🇬", "Bulgarian"},
	"bg":  {"🇧🇬", "Bulgarian"},

	// Croatian
	"hrv": {"🇭🇷", "Croatian"},
	"scr": {"🇭🇷", "Croatian"},
	"hr":  {"🇭🇷", "Croatian"},

	// Serbian
	"srp": {"🇷🇸", "Serbian"},
	"scc": {"🇷🇸", "Serbian"},
	"sr":  {"🇷🇸", "Serbian"},

	// Slovenian
	"slv": {"🇸🇮", "Slovenian"},
	"sl":  {"🇸🇮", "Slovenian"},

	// Estonian
	"est": {"🇪🇪", "Estonian"},
	"et":  {"🇪🇪", "Estonian"},

	// Latvian
	"lav": {"🇱🇻", "Latvian"},
	"lv":  {"🇱🇻", "Latvian"},

	// Lithuanian
	"lit": {"🇱🇹", "Lithuanian"},
	"lt":  {"🇱🇹", "Lithuanian"},

	// Icelandic
	"isl": {"🇮🇸", "Icelandic"},
	"ice": {"🇮🇸", "Icelandic"},
	"is":  {"🇮🇸", "Icelandic"},

	// Japanese
	"jpn": {"🇯🇵", "Japanese"},
	"ja":  {"🇯🇵", "Japanese"},

	// Chinese
	"chi":   {"🇨🇳", "Chinese"},
	"zho":   {"🇨🇳", "Chinese"},
	"zh":    {"🇨🇳", "Chinese"},
	"zh-cn": {"🇨🇳", "Chinese (Simp)"},
	"zh_cn": {"🇨🇳", "Chinese (Simp)"},
	"zh-tw": {"🇹🇼", "Chinese (Trad)"},
	"zh_tw": {"🇹🇼", "Chinese (Trad)"},

	// Korean
	"kor": {"🇰🇷", "Korean"},
	"ko":  {"🇰🇷", "Korean"},

	// Arabic
	"ara": {"🇸🇦", "Arabic"},
	"ar":  {"🇸🇦", "Arabic"},

	// Hebrew
	"heb": {"🇮🇱", "Hebrew"},
	"he":  {"🇮🇱", "Hebrew"},

	// Persian
	"fas": {"🇮🇷", "Persian"},
	"per": {"🇮🇷", "Persian"},
	"fa":  {"🇮🇷", "Persian"},

	// Hindi
	"hin": {"🇮🇳", "Hindi"},
	"hi":  {"🇮🇳", "Hindi"},

	// Sinhala / Sinhalese
	"sin": {"🇱🇰", "Sinhala"},
	"si":  {"🇱🇰", "Sinhala"},

	// Vietnamese
	"vie": {"🇻🇳", "Vietnamese"},
	"vi":  {"🇻🇳", "Vietnamese"},

	// Thai
	"tha": {"🇹🇭", "Thai"},
	"th":  {"🇹🇭", "Thai"},

	// Indonesian
	"ind": {"🇮🇩", "Indonesian"},
	"id":  {"🇮🇩", "Indonesian"},

	// Catalan
	"cat": {"🎗", "Catalan"},
	"ca":  {"🎗", "Catalan"},
}
