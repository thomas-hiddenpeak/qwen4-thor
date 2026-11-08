//! Bounded regular-language intersection and independent matching for schemas.
//! This does not change the existing custom-tool regex converter.
use super::{accepts, bound, categories, charset, intersect, Buffer, Edge, Ebnf,
            Nfa, Ranges, Result, LIMIT};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::fmt::Write;

const WORK: usize = 8 * 1024 * 1024;
fn charge(work: &mut usize, amount: usize) -> Result<()> {
    if amount > *work { return Err(2); }
    *work -= amount; Ok(())
}
fn contains(ranges: &Ranges, cp: u32) -> bool {
    let i = ranges.partition_point(|&(_, high)| high < cp);
    i < ranges.len() && ranges[i].0 <= cp
}
struct Graph { nfa: Nfa, start: usize, end: usize }
impl Graph {
    fn has_assertions(&self) -> bool {
        self.nfa.edges.iter().flatten().any(|edge| matches!(edge,Edge::Look(..)))
    }
    fn categories(&self,other: Option<&Self>) -> Vec<Ranges> {
        if self.has_assertions() || other.is_some_and(Self::has_assertions) {
            categories()
        } else {
            vec![vec![],vec![(0,0xd7ff),(0xe000,0x10ffff)]]
        }
    }
    fn empty() -> Result<Self> {
        let mut nfa=Nfa::default(); let start=nfa.node()?; let end=nfa.node()?;
        Ok(Self { nfa,start,end })
    }
    fn parse(pattern: &str) -> Result<Self> {
        bound(pattern.len(),16384)?;
        let hir=regex_syntax::ParserBuilder::new().nest_limit(512).build()
            .parse(pattern).map_err(|error| match error {
                regex_syntax::Error::Parse(error) if matches!(error.kind(),
                    regex_syntax::ast::ErrorKind::NestLimitExceeded(_)) => 2u32,
                _ => 1u32,
            })?;
        let mut graph=Self::empty()?;
        graph.nfa.build(&hir,graph.start,graph.end)?;
        Ok(graph)
    }
    fn closure(&self,states: impl IntoIterator<Item=usize>,previous: usize,next: usize,
               work: &mut usize) -> Result<BTreeSet<usize>> {
        let mut seen=BTreeSet::new(); let mut pending: Vec<usize>=states.into_iter().collect();
        while let Some(state)=pending.pop() {
            charge(work,1)?;
            if !seen.insert(state) { continue; }
            for edge in &self.nfa.edges[state] {
                charge(work,1)?;
                match edge {
                    Edge::Empty(to)=>pending.push(*to),
                    Edge::Look(look,to) if accepts(*look,previous,next)=>pending.push(*to),
                    _=>{},
                }
            }
        }
        Ok(seen)
    }
    fn transitions(&self,state: usize,previous: usize,next: usize,work: &mut usize)
                   -> Result<Vec<(&Ranges,usize)>> {
        let mut transitions=vec![];
        for at in self.closure([state],previous,next,work)? {
            for edge in &self.nfa.edges[at] {
                charge(work,1)?;
                if let Edge::Char(r,to)=edge { transitions.push((r,*to)); }
            }
        }
        Ok(transitions)
    }
    fn intersection(&self,other: &Self,work: &mut usize) -> Result<Self> {
        // Construct reachable pairs of NFA subsets. A pair of individual NFA
        // states retains redundant paths at every intersection and can exhaust
        // the bound on ordinary decimal ranges. This bounded subset product
        // preserves assertion context while merging equivalent active paths.
        type Key=(Vec<usize>,Vec<usize>,usize);
        let cats=self.categories(Some(other)); let mut output=Self::empty()?;
        let first: Key=(vec![self.start],vec![other.start],0);
        let mut nodes=BTreeMap::from([(first.clone(),output.start)]);
        let mut pending=VecDeque::from([first]); let mut edge_count=0;
        while let Some(key)=pending.pop_front() {
            charge(work,1)?;
            let current=nodes[&key]; let (a,b,prev)=key;
            let mut combined: BTreeMap<usize,Ranges>=BTreeMap::new();
            if self.closure(a.iter().copied(),prev,0,work)?.contains(&self.end) &&
               other.closure(b.iter().copied(),prev,0,work)?.contains(&other.end) {
                output.nfa.edges[current].push(Edge::Empty(output.end));
            }
            for (next,cat) in cats.iter().enumerate().skip(1) {
                // Sweep disjoint Unicode intervals, maintaining destination
                // multiplicities for overlapping transitions to the same node.
                let mut events: BTreeMap<u32,Vec<(usize,usize,i32)>>=BTreeMap::new();
                for (side,(graph,states)) in [(self,&a),(other,&b)].iter().enumerate() {
                    let closure=graph.closure(states.iter().copied(),prev,next,work)?;
                    for at in closure { for edge in &graph.nfa.edges[at] {
                        charge(work,1)?;
                        if let Edge::Char(r,to)=edge {
                            charge(work,r.len()+cat.len())?;
                            for (low,high) in intersect(r,cat) {
                                charge(work,2)?;
                                events.entry(low).or_default().push((side,*to,1));
                                events.entry(high+1).or_default().push((side,*to,-1));
                            }
                        }
                    } }
                }
                let mut active=[BTreeMap::<usize,usize>::new(),BTreeMap::new()];
                let mut previous=0;
                for (cp,changes) in events {
                    if cp>previous && !active[0].is_empty() && !active[1].is_empty() {
                        charge(work,active[0].len()+active[1].len())?;
                        let target: Key=(active[0].keys().copied().collect(),
                                         active[1].keys().copied().collect(),next);
                        let to=if let Some(to)=nodes.get(&target) { *to } else {
                            let to=output.nfa.node()?;
                            nodes.insert(target.clone(),to); pending.push_back(target); to
                        };
                        let ranges=combined.entry(to).or_default();
                        if let Some(last)=ranges.last_mut() {
                            if last.1+1==previous { last.1=cp-1; }
                            else { ranges.push((previous,cp-1)); }
                        } else { ranges.push((previous,cp-1)); }
                    }
                    for (side,to,delta) in changes {
                        charge(work,1)?;
                        let counts=&mut active[side];
                        if delta>0 { *counts.entry(to).or_default()+=1; }
                        else if let Some(count)=counts.get_mut(&to) {
                            *count-=1; if *count==0 { counts.remove(&to); }
                        } else { return Err(3); }
                    }
                    previous=cp;
                }
            }
            for (to,ranges) in combined {
                edge_count+=1; bound(edge_count,65536)?;
                output.nfa.edges[current].push(Edge::Char(ranges,to));
            }
        }
        Ok(output)
    }
    fn matches(&self,text: &str,work: &mut usize) -> Result<bool> {
        charge(work,text.len())?;
        let cats=self.categories(None); let mut states=BTreeSet::from([self.start]); let mut prev=0;
        for c in text.chars() {
            let cp=c as u32;
            let next=(1..cats.len()).find(|&i| contains(&cats[i],cp)).ok_or(3u32)?;
            let closure=self.closure(states,prev,next,work)?;
            states=BTreeSet::new();
            for at in closure { for edge in &self.nfa.edges[at] {
                charge(work,1)?;
                if let Edge::Char(r,to)=edge {
                    if contains(r,cp) { states.insert(*to); }
                }
            } }
            if states.is_empty() { return Ok(false); }
            prev=next;
        }
        Ok(self.closure(states,prev,0,work)?.contains(&self.end))
    }
    fn ebnf(&self,work: &mut usize) -> Result<String> {
        let cats=self.categories(None); let mut pending=VecDeque::from([(self.start,0)]);
        let mut seen=BTreeSet::new(); let mut out=Ebnf::default();
        let name=|state,prev|format!("s{state}p{prev}");
        while let Some((state,prev))=pending.pop_front() {
            charge(work,1)?;
            if !seen.insert((state,prev)) { continue; }
            let mut branches=BTreeSet::new();
            if self.closure([state],prev,0,work)?.contains(&self.end) {
                branches.insert("\"\"".to_string());
            }
            for (next,cat) in cats.iter().enumerate().skip(1) {
                for (r,to) in self.transitions(state,prev,next,work)? {
                    charge(work,r.len()+cat.len())?;
                    let ranges=intersect(r,cat); if ranges.is_empty() { continue; }
                    let class=out.class(ranges)?;
                    branches.insert(format!("{class} {}",name(to,next)));
                    pending.push_back((to,next));
                }
            }
            let body=if branches.is_empty() { charset(&vec![]) }
                     else { branches.into_iter().collect::<Vec<_>>().join(" | ") };
            writeln!(&mut out.rules,"{} ::= ({body})",name(state,prev)).map_err(|_|3u32)?;
            bound(out.rules.len(),LIMIT)?;
        }
        let result=format!("root ::= {}\n{}",name(self.start,0),out.rules);
        bound(result.len(),LIMIT)?; Ok(result)
    }
    // Accept decimal spellings whose exact value is a multiple of
    // coefficient * 10^-scale. The base number language is intersected later,
    // so this graph can leave leading-zero/dot syntax to that independent rule.
    fn multiple(coefficient: u32,scale: i32) -> Result<Self> {
        if coefficient==0 { return Err(1); }
        bound(coefficient as usize,4096)?; bound(scale.unsigned_abs() as usize,16384)?;
        let mut graph=Self::empty()?;
        let mut modulo=Vec::new();
        for _ in 0..coefficient { modulo.push(graph.nfa.node()?); }
        graph.nfa.edges[graph.start].push(Edge::Empty(modulo[0]));
        graph.nfa.edges[graph.start].push(Edge::Char(vec![(45,45)],modulo[0]));
        for rem in 0..coefficient {
            for d in 0..10 {
                let next=(rem*10+d)%coefficient;
                graph.nfa.edges[modulo[rem as usize]].push(
                    Edge::Char(vec![(48+d,48+d)],modulo[next as usize]));
            }
        }
        if scale < 0 {
            // Reserve the required zero suffix after a divisible integer
            // prefix; nondeterminism avoids an exponentially large modulus.
            let tail=graph.nfa.node()?;
            let mut at=modulo[0];
            for i in 0..(-scale) {
                let next=if i+1==-scale { tail } else { graph.nfa.node()? };
                graph.nfa.edges[at].push(Edge::Char(vec![(48,48)],next)); at=next;
            }
            // Zero is a multiple even without a suffix of the requested size.
            graph.nfa.edges[graph.start].push(Edge::Char(vec![(48,48)],tail));
            let signed_zero=graph.nfa.node()?;
            graph.nfa.edges[graph.start].push(Edge::Char(vec![(45,45)],signed_zero));
            graph.nfa.edges[signed_zero].push(Edge::Char(vec![(48,48)],tail));
            graph.nfa.edges[tail].push(Edge::Empty(graph.end));
            let decimal=graph.nfa.node()?;
            graph.nfa.edges[tail].push(Edge::Char(vec![(46,46)],decimal));
            graph.nfa.edges[decimal].push(Edge::Char(vec![(48,48)],decimal));
            graph.nfa.edges[decimal].push(Edge::Empty(graph.end));
            return Ok(graph);
        }
        let scale=scale as usize;
        let mut powers=vec![1%coefficient];
        for i in 0..scale { powers.push((powers[i]*10)%coefficient); }
        let mut fractions=Vec::new();
        for _ in 0..=scale {
            let mut row=Vec::new();
            for _ in 0..coefficient { row.push(graph.nfa.node()?); }
            fractions.push(row);
        }
        for rem in 0..coefficient {
            let index=rem as usize;
            if (rem*powers[scale])%coefficient==0 {
                graph.nfa.edges[modulo[index]].push(Edge::Empty(graph.end));
            }
            graph.nfa.edges[modulo[index]].push(Edge::Char(vec![(46,46)],fractions[0][index]));
            for n in 0..=scale {
                if (rem*powers[scale-n])%coefficient==0 {
                    graph.nfa.edges[fractions[n][index]].push(Edge::Empty(graph.end));
                }
                if n < scale {
                    for d in 0..10 {
                        let next=((rem*10+d)%coefficient) as usize;
                        graph.nfa.edges[fractions[n][index]].push(
                            Edge::Char(vec![(48+d,48+d)],fractions[n+1][next]));
                    }
                }
            }
        }
        // Extra decimal places may contain only zero after exact divisibility.
        graph.nfa.edges[fractions[scale][0]].push(Edge::Char(vec![(48,48)],fractions[scale][0]));
        Ok(graph)
    }
}

unsafe fn input<'a>(data: *const u8,size: usize,limit: usize) -> Result<&'a str> {
    if data.is_null() { return Err(1); } bound(size,limit)?;
    std::str::from_utf8(std::slice::from_raw_parts(data,size)).map_err(|_|1u32)
}
fn buffer(result: Result<String>) -> Buffer {
    match result {
        Ok(text)=> {
            let bytes=text.into_bytes().into_boxed_slice(); let size=bytes.len();
            Buffer { data:Box::into_raw(bytes) as *mut u8,size,error:0 }
        },
        Err(error)=>Buffer { data:std::ptr::null_mut(),size:0,error },
    }
}

#[no_mangle]
pub unsafe extern "C" fn q4t_schema_regex_ebnf(
    patterns: *const *const u8,sizes: *const usize,count: usize,
    multiple: u32,scale: i32
) -> Buffer {
    buffer(std::panic::catch_unwind(|| {
        if patterns.is_null() || sizes.is_null() || count==0 || count>8 { return Err(1); }
        let patterns=std::slice::from_raw_parts(patterns,count);
        let sizes=std::slice::from_raw_parts(sizes,count);
        let mut graph=Graph::parse(input(patterns[0],sizes[0],16384)?)?;
        let mut work=WORK;
        for i in 1..count {
            let next=Graph::parse(input(patterns[i],sizes[i],16384)?)?;
            graph=graph.intersection(&next,&mut work)?;
        }
        if multiple>0 { graph=graph.intersection(&Graph::multiple(multiple,scale)?,&mut work)?; }
        graph.ebnf(&mut work)
    }).unwrap_or(Err(3)))
}

#[repr(C)]
pub struct Match { matched: u32,error: u32 }
#[no_mangle]
pub unsafe extern "C" fn q4t_schema_regex_matches(
    pattern: *const u8,pattern_size: usize,text: *const u8,text_size: usize,
    work: *mut usize
) -> Match {
    let result=std::panic::catch_unwind(|| {
        if work.is_null() { return Err(1); }
        let pattern=input(pattern,pattern_size,16384)?;
        let text=input(text,text_size,1024*1024)?;
        charge(&mut *work,pattern.len())?;
        Graph::parse(pattern)?.matches(text,&mut *work)
    }).unwrap_or(Err(3));
    match result { Ok(matched)=>Match { matched:u32::from(matched),error:0 },
                   Err(error)=>Match { matched:0,error } }
}
