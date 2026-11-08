//! Rust regex syntax to a Unicode EBNF language. No model or tokenizer state.
//! The C++ grammar worker applies its process time and address-space limits.
use regex_syntax::hir::{Class, Hir, HirKind, Look};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::fmt::Write;
mod schema_regex;

const LIMIT: usize = 512 * 1024;
const NODES: usize = 4096;
type Ranges = Vec<(u32, u32)>;
type Result<T> = std::result::Result<T, u32>;
fn bound(n: usize, max: usize) -> Result<()> { if n > max { Err(2) } else { Ok(()) } }
fn cp(c: u32) -> String {
    if c <= 65535 { format!("\\u{c:04x}") } else { format!("\\U{c:08x}") }
}
fn charset(r: &Ranges) -> String {
    if r.is_empty() { return "[^\\u0000-\\U0010ffff]".into(); }
    let mut s = String::from("[");
    for &(a,b) in r { s += &cp(a); if a != b { s += "-"; s += &cp(b); } }
    s + "]"
}
fn class(c: &Class) -> Ranges {
    match c {
        Class::Unicode(v) => v.iter().map(|r| (r.start() as u32,r.end() as u32)).collect(),
        Class::Bytes(v) => v.iter().map(|r| (r.start() as u32,r.end() as u32)).collect(),
    }
}
fn intersect(a: &Ranges,b: &Ranges) -> Ranges {
    let (mut i,mut j) = (0,0); let mut out = vec![];
    while i < a.len() && j < b.len() {
        let (lo,hi) = (a[i].0.max(b[j].0),a[i].1.min(b[j].1));
        if lo <= hi { out.push((lo,hi)); }
        if a[i].1 < b[j].1 { i+=1; } else { j+=1; }
    }
    out
}
fn subtract(a: &Ranges,b: &Ranges) -> Ranges {
    let mut out = vec![];
    for &(lo,hi) in a {
        let mut at=lo;
        for &(l,h) in b {
            if h < at { continue; } if l > hi { break; }
            if l > at { out.push((at,l-1)); } at=at.max(h+1);
        }
        if at <= hi { out.push((at,hi)); }
    }
    out
}
fn has_look(h: &Hir) -> bool {
    matches!(h.kind(),HirKind::Look(_)) || h.kind().subs().iter().any(has_look)
}
#[derive(Default)]
struct Ebnf { rules: String, count: usize, classes: BTreeMap<Ranges,String> }
impl Ebnf {
    fn rule(&mut self,body: &str) -> Result<String> {
        self.count+=1; bound(self.count,32768)?;
        let name=format!("r{}",self.count);
        writeln!(&mut self.rules,"{name} ::= {body}").map_err(|_| 3u32)?;
        bound(self.rules.len(),LIMIT)?; Ok(name)
    }
    fn class(&mut self,r: Ranges) -> Result<String> {
        if let Some(name)=self.classes.get(&r) { return Ok(name.clone()); }
        let name=self.rule(&charset(&r))?; self.classes.insert(r,name.clone()); Ok(name)
    }
    fn hir(&mut self,h: &Hir) -> Result<String> {
        let body=match h.kind() {
            HirKind::Empty => "\"\"".into(),
            HirKind::Literal(v) => {
                let text=std::str::from_utf8(&v.0).map_err(|_|1u32)?;
                let parts: Vec<String>=text.chars().map(|c| format!("\"{}\"",cp(c as u32))).collect();
                if parts.is_empty() { "\"\"".into() } else { parts.join(" ") }
            },
            HirKind::Class(c) => return self.class(class(c)),
            HirKind::Capture(c) => return self.hir(&c.sub),
            HirKind::Repetition(r) => {
                let sub=self.hir(&r.sub)?;
                if r.max==Some(r.min) { format!("{sub}{{{}}}",r.min) }
                else { format!("{sub}{{{},{}}}",r.min,r.max.map(|n|n.to_string()).unwrap_or_default()) }
            },
            HirKind::Concat(v) | HirKind::Alternation(v) => {
                let parts: Result<Vec<String>>=v.iter().map(|h|self.hir(h)).collect();
                let sep=if matches!(h.kind(),HirKind::Concat(_)) { " " } else { " | " };
                format!("({})",parts?.join(sep))
            },
            HirKind::Look(_) => return Err(3),
        };
        self.rule(&body)
    }
}
// A Thompson graph over Unicode scalars. Assertion closure depends only on
// the previous/next character category, never on unbounded backtracking.
#[derive(Clone)]
enum Edge { Empty(usize), Look(Look,usize), Char(Ranges,usize) }
#[derive(Default)]
struct Nfa { edges: Vec<Vec<Edge>> }
impl Nfa {
    fn node(&mut self) -> Result<usize> {
        bound(self.edges.len()+1,NODES)?; self.edges.push(vec![]); Ok(self.edges.len()-1)
    }
    fn build(&mut self,h: &Hir,a: usize,b: usize) -> Result<()> {
        match h.kind() {
            HirKind::Empty => self.edges[a].push(Edge::Empty(b)),
            HirKind::Look(l) => self.edges[a].push(Edge::Look(*l,b)),
            HirKind::Class(c) => self.edges[a].push(Edge::Char(class(c),b)),
            HirKind::Literal(v) => {
                let chars: Vec<char>=std::str::from_utf8(&v.0).map_err(|_|1u32)?.chars().collect();
                let mut at=a;
                for (i,c) in chars.iter().enumerate() {
                    let next=if i+1==chars.len() { b } else { self.node()? };
                    self.edges[at].push(Edge::Char(vec![(*c as u32,*c as u32)],next)); at=next;
                }
                if chars.is_empty() { self.edges[a].push(Edge::Empty(b)); }
            },
            HirKind::Capture(c) => self.build(&c.sub,a,b)?,
            HirKind::Concat(v) => {
                let mut at=a;
                for (i,sub) in v.iter().enumerate() {
                    let next=if i+1==v.len() { b } else { self.node()? };
                    self.build(sub,at,next)?; at=next;
                }
            },
            HirKind::Alternation(v) => for sub in v { self.build(sub,a,b)?; },
            HirKind::Repetition(r) => {
                bound(r.max.unwrap_or(r.min) as usize,NODES)?;
                let mut at=a;
                for _ in 0..r.min { let next=self.node()?; self.build(&r.sub,at,next)?; at=next; }
                self.edges[at].push(Edge::Empty(b));
                if let Some(max)=r.max {
                    for _ in r.min..max {
                        let next=self.node()?; self.build(&r.sub,at,next)?; at=next;
                        self.edges[at].push(Edge::Empty(b));
                    }
                } else { let next=self.node()?; self.build(&r.sub,at,next)?; self.edges[next].push(Edge::Empty(at)); }
            },
        }
        Ok(())
    }
    fn closure(&self,state: usize,prev: usize,next: usize) -> BTreeSet<usize> {
        let mut seen=BTreeSet::new(); let mut work=vec![state];
        while let Some(at)=work.pop() {
            if !seen.insert(at) { continue; }
            for edge in &self.edges[at] { match edge {
                Edge::Empty(to) => work.push(*to),
                Edge::Look(l,to) if accepts(*l,prev,next) => work.push(*to),
                _=>{},
            } }
        }
        seen
    }
}
// 0 = text boundary, 1 = LF, 2 = CR, 3 = ASCII word, 4 = other
// Unicode word, 5 = nonword. They partition valid Unicode scalar values.
fn categories() -> Vec<Ranges> {
    let word=regex_syntax::parse(r"\w").expect("constant word class");
    let HirKind::Class(w)=word.kind() else { unreachable!() };
    let w=class(w); let ascii=vec![(48,57),(65,90),(95,95),(97,122)];
    let nonword=subtract(&subtract(&vec![(0,0xd7ff),(0xe000,0x10ffff)],&w),&vec![(10,10),(13,13)]);
    vec![vec![],vec![(10,10)],vec![(13,13)],ascii.clone(),subtract(&w,&ascii),nonword]
}
fn accepts(l: Look,p: usize,n: usize) -> bool {
    let (pa,na)=(p==3,n==3); let (pu,nu)=((3..=4).contains(&p),(3..=4).contains(&n));
    match l {
        Look::Start=>p==0, Look::End=>n==0,
        Look::StartLF=>p==0||p==1, Look::EndLF=>n==0||n==1,
        Look::StartCRLF=>p==0||p==1||(p==2&&n!=1),
        Look::EndCRLF=>n==0||n==2||(n==1&&p!=2),
        Look::WordAscii=>pa!=na, Look::WordAsciiNegate=>pa==na,
        Look::WordUnicode=>pu!=nu, Look::WordUnicodeNegate=>pu==nu,
        Look::WordStartAscii=>!pa&&na, Look::WordEndAscii=>pa&&!na,
        Look::WordStartUnicode=>!pu&&nu, Look::WordEndUnicode=>pu&&!nu,
        Look::WordStartHalfAscii=>!pa, Look::WordEndHalfAscii=>!na,
        Look::WordStartHalfUnicode=>!pu, Look::WordEndHalfUnicode=>!nu,
    }
}
fn with_look(h: &Hir,e: &mut Ebnf) -> Result<String> {
    let mut nfa=Nfa::default(); let start=nfa.node()?; let end=nfa.node()?;
    nfa.build(h,start,end)?;
    let cats=categories(); let mut queue=VecDeque::from([(start,0)]); let mut seen=BTreeSet::new();
    let name=|s,p|format!("n{s}p{p}");
    let mut steps=0;
    while let Some((state,prev))=queue.pop_front() {
        if !seen.insert((state,prev)) { continue; }
        let mut branches=BTreeSet::new();
        if nfa.closure(state,prev,0).contains(&end) { branches.insert("\"\"".into()); }
        for (next,cat) in cats.iter().enumerate().skip(1) {
            for at in nfa.closure(state,prev,next) {
                for edge in &nfa.edges[at] {
                    steps+=1; bound(steps,2_000_000)?;
                    if let Edge::Char(r,to)=edge {
                        let intersection=intersect(r,cat); if intersection.is_empty() { continue; }
                        let c=e.class(intersection)?;
                        branches.insert(format!("{c} {}",name(*to,next))); queue.push_back((*to,next));
                    }
                }
            }
        }
        let body=if branches.is_empty() { charset(&vec![]) } else { branches.into_iter().collect::<Vec<_>>().join(" | ") };
        writeln!(&mut e.rules,"{} ::= ({body})",name(state,prev)).map_err(|_|3u32)?;
        bound(e.rules.len(),LIMIT)?;
    }
    Ok(name(start,0))
}
fn convert(pattern: &str) -> Result<String> {
    bound(pattern.len(),16384)?;
    let hir=regex_syntax::ParserBuilder::new().nest_limit(64).build().parse(pattern).map_err(|_|1u32)?;
    let mut ebnf=Ebnf::default();
    let root=if has_look(&hir) { with_look(&hir,&mut ebnf)? } else { ebnf.hir(&hir)? };
    let result=format!("root ::= {root}\n{}",ebnf.rules); bound(result.len(),LIMIT)?; Ok(result)
}
#[repr(C)]
pub struct Buffer { data: *mut u8, size: usize, error: u32 }
#[no_mangle]
pub unsafe extern "C" fn q4t_regex_ebnf(data: *const u8,size: usize) -> Buffer {
    let result=std::panic::catch_unwind(|| {
        if data.is_null() || size>16384 { return Err(1); }
        let text=std::str::from_utf8(std::slice::from_raw_parts(data,size)).map_err(|_|1u32)?;
        convert(text)
    }).unwrap_or(Err(3));
    match result {
        Ok(text)=> { let bytes=text.into_bytes().into_boxed_slice(); let size=bytes.len();
            Buffer { data: Box::into_raw(bytes) as *mut u8,size,error:0 } },
        Err(error)=> Buffer { data:std::ptr::null_mut(),size:0,error },
    }
}
#[no_mangle]
pub unsafe extern "C" fn q4t_regex_free(buffer: Buffer) {
    if !buffer.data.is_null() {
        drop(Box::from_raw(std::ptr::slice_from_raw_parts_mut(buffer.data,buffer.size)));
    }
}
