// Deterministic resume template. Reads merged data from data.json (written next to
// this file at render time) and lays out a clean, ATS-parseable one/two-page PDF.
// The template owns ALL layout — tailoring only supplies text, never styling.
// Style: single-column, tight spacing, "Jake's Resume" school. Section order:
// Summary -> Skills -> Experience -> Education (education last, by design).

#let data = json("data.json")

#set document(title: data.name + " — Resume", author: data.name)
#set page(paper: "us-letter", margin: (x: 0.5in, top: 0.45in, bottom: 0.45in))
#set text(font: ("Helvetica Neue", "Helvetica", "Arial"), size: 10pt, fill: rgb("#1a1d21"))
// leading = space within a paragraph; spacing = space BETWEEN blocks/paragraphs.
// The old template left `spacing` at its ~1.2em default — that was the "间距太大".
#set par(justify: false, leading: 0.5em, spacing: 0.5em)

#let navy = rgb("#12314f")

// --- header ---
#align(center)[
  #text(size: 20pt, weight: "bold", fill: navy)[#data.name]
  #v(3pt, weak: true)
  #let c = data.contact
  #text(size: 9pt)[
    // Optional fields render only when present. This lets a candidate deliberately keep the
    // public header to phone + email without leaving empty separators or broken links.
    #let loc = c.at("location", default: "")
    #let li = c.at("linkedin", default: "")
    #let gh = c.at("github", default: "")
    #if gh != none and gh != "" [
      #c.phone #h(5pt) · #h(5pt) #link("mailto:" + c.email)[#c.email]
      #if loc != none and loc != "" [#h(5pt) · #h(5pt) #loc]
      \
      #if li != none and li != "" [#link("https://www." + li + "/")[#li] #h(5pt) · #h(5pt)]
      #link("https://" + gh)[#gh]
    ] else [
      #c.phone #h(5pt) · #h(5pt) #link("mailto:" + c.email)[#c.email]
      #if loc != none and loc != "" [#h(5pt) · #h(5pt) #loc]
      #if li != none and li != "" [#h(5pt) · #h(5pt) #link("https://www." + li + "/")[#li]]
    ]
  ]
]
#v(3pt)

#let section(title) = {
  v(5pt)                       // breathing room between sections (≈ font-size 5)
  text(size: 11.5pt, weight: "bold", fill: navy)[#upper(title)]
  v(2pt, weak: true)
  line(length: 100%, stroke: 0.6pt + navy)
  v(3pt, weak: true)
}

#let entry(left, right, sub) = {
  grid(columns: (1fr, auto),
    text(weight: "bold")[#left], text(weight: "bold")[#right])
  if sub != none [ #text(style: "italic", size: 9.5pt)[#sub] ]
}

// --- summary (tailored; optional) ---
#if "summary" in data and data.summary != none and data.summary != "" [
  #section("Summary")
  #data.summary
]

// --- skills ---
#section("Skills")
#stack(spacing: 5.5pt, ..data.skills.map(group => [
  #text(weight: "bold")[#group.label: ] #group.items.join(", ")
]))

// --- optional projects ---
// A role-specific resume may anchor the section after a named company. This lets a career-
// direction project sit immediately after the recent experience that establishes its context,
// without changing the default order for other resumes.
#let projects = data.at("projects", default: ())
#let projects_after_company = data.at("projects_after_company", default: "")
#let projects_start_new_page = data.at("projects_start_new_page", default: false)
#let project_section = [
  #section("Projects")
  #for pr in projects [
    #block(breakable: false)[
      #grid(columns: (1fr, auto),
        [#text(weight: "bold")[#pr.name] #if pr.at("link", default: "") != "" [ #h(5pt) #text(size: 9pt)[#link(pr.link)[#pr.at("link_label", default: pr.link)]] ]],
        text(weight: "bold")[#pr.at("year", default: "")])
      #if pr.at("stack", default: "") != "" [ #v(2pt) #text(style: "italic", size: 9.5pt)[#pr.stack] ]
      #v(4pt)
      #set list(marker: text(fill: navy)[•], indent: 2pt, body-indent: 6pt, spacing: 3.5pt)
      #list(..pr.bullets.map(b => [#b]))
      #v(5pt)
    ]
  ]
]

// --- experience ---
#section("Professional Experience")
#for job in data.experience [
  #block(breakable: false)[
    #grid(columns: (1fr, auto),
      text(weight: "bold")[#job.company], text(weight: "bold")[#job.dates])
    #v(2.5pt)                              // gap under the company line
    #grid(columns: (1fr, auto),
      text(style: "italic", size: 9.5pt)[#job.title],
      text(style: "italic", size: 9.5pt)[#job.location])
    #v(4.5pt)                              // gap under the title/location line, before bullets
    #set list(marker: text(fill: navy)[•], indent: 2pt, body-indent: 6pt, spacing: 3.5pt)
    #list(..job.bullets.map(b => [#b]))
    #v(5pt)                      // gap between experience entries (≈ font-size 5)
  ]
  #if projects.len() > 0 and projects_after_company != "" and job.company == projects_after_company [
    #if projects_start_new_page [#pagebreak()]
    #project_section
    #if data.at("additional_experience_heading", default: "") != "" [
      #section(data.additional_experience_heading)
    ]
  ]
]

// Default placement for resumes that do not set a company anchor.
#if projects.len() > 0 and projects_after_company == "" [
  #project_section
]

// --- education (last, by design) ---
#section("Education")
#stack(spacing: 5pt, ..data.education.map(e => entry(e.school, e.dates, e.degree)))
