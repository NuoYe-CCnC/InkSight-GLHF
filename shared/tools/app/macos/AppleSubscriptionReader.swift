// Public AX reader runs inside the final InkSight process; no test-app permission reuse.
import AppKit
import Foundation
import CoreGraphics
import ApplicationServices

func iso(_ d:Date=Date()) -> String { let f=ISO8601DateFormatter();f.formatOptions=[.withInternetDateTime,.withFractionalSeconds];f.timeZone=TimeZone(identifier:"Asia/Shanghai");return f.string(from:d) }
func save(_ j:[String:Any],_ url:URL) { if let data=try? JSONSerialization.data(withJSONObject:j,options:[.sortedKeys,.prettyPrinted]) { try? data.write(to:url,options:.atomic) } }
func windows() -> [[String:Any]]? { CGWindowListCopyWindowInfo([.optionAll,.excludeDesktopElements],kCGNullWindowID) as? [[String:Any]] }
func readAX(_ e:AXUIElement,_ key:String) -> CFTypeRef? { var v:CFTypeRef?;if AXUIElementCopyAttributeValue(e,key as CFString,&v) != .success { return nil };return v }
func frameAX(_ e:AXUIElement) -> CGRect? {
 guard let p=readAX(e,kAXPositionAttribute),let s=readAX(e,kAXSizeAttribute),CFGetTypeID(p)==AXValueGetTypeID(),CFGetTypeID(s)==AXValueGetTypeID() else { return nil }
 var point=CGPoint.zero;var size=CGSize.zero
 guard AXValueGetValue(unsafeBitCast(p,to:AXValue.self),.cgPoint,&point),AXValueGetValue(unsafeBitCast(s,to:AXValue.self),.cgSize,&size) else { return nil };return CGRect(origin:point,size:size)
}
struct Phrase:Equatable { let wording:String;let date:String;let action:String;var json:[String:String] { ["product":"ChatGPT Plus","renewal_or_expiry_wording":wording,"display_date":date,"action_word":action,"precision":"displayed calendar date; no payment time or missing year inferred"] } }
func phrases(_ text:String) -> [Phrase] {
 let patterns=["(?:[0-9]{4}年\\s*)?[0-9]{1,2}月\\s*[0-9]{1,2}日\\s*(?:续期|续订|到期)","(?:续期|续订|到期)(?:日期)?[：:]?\\s*(?:[0-9]{4}年\\s*)?[0-9]{1,2}月\\s*[0-9]{1,2}日","(?i)(?:Renews|Expires|Expired)(?: on)?\\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\\s+[0-9]{1,2}(?:,?\\s+[0-9]{4})?"]
 var out:[Phrase]=[]
 for pattern in patterns { guard let re=try? NSRegularExpression(pattern:pattern) else { continue };for m in re.matches(in:text,range:NSRange(text.startIndex...,in:text)) { guard let rr=Range(m.range,in:text) else { continue };let w=String(text[rr]);let word=["续期","续订","到期","Renews","Expires","Expired"].first { w.range(of:$0,options:.caseInsensitive) != nil } ?? ""
   let date=w.replacingOccurrences(of:"(?i)(?:续期|续订|到期)(?:日期)?[：:]?|(?:Renews|Expires|Expired)(?: on)?",with:"",options:.regularExpression).trimmingCharacters(in:.whitespacesAndNewlines)
   if let re=try? NSRegularExpression(pattern:"(?:([0-9]{4})年\\s*)?([0-9]{1,2})月\\s*([0-9]{1,2})日"),let dm=re.firstMatch(in:date,range:NSRange(date.startIndex...,in:date)),let mr=Range(dm.range(at:2),in:date),let dr=Range(dm.range(at:3),in:date),let mon=Int(date[mr]),let day=Int(date[dr]) {
    let year=Range(dm.range(at:1),in:date).flatMap { Int(date[$0]) } ?? 2000
    var cal=Calendar(identifier:.gregorian);cal.timeZone=TimeZone(secondsFromGMT:0)!;guard let value=cal.date(from:DateComponents(year:year,month:mon,day:day)),cal.component(.month,from:value)==mon,cal.component(.day,from:value)==day else { continue }
   }
   if (word=="续期" || word=="续订" || word.lowercased()=="renews") && text.range(of:"已取消|不会续|will not renew|cancelled|canceled",options:[.regularExpression,.caseInsensitive]) != nil { continue }
   let p=Phrase(wording:w,date:date,action:word);if !out.contains(p) { out.append(p) }
  } }
 return out
}
func hasPlus(_ s:String) -> Bool { s.range(of:"ChatGPT\\s*Plus",options:[.regularExpression,.caseInsensitive]) != nil }
func directCandidate(_ s:String) -> Phrase? { guard hasPlus(s) else { return nil };let p=phrases(s);return p.count==1 ? p[0] : nil }
final class Node {
 weak var parent:Node?;var fixtureFrame:CGRect?;var geometry:CGRect? { fixtureFrame ?? frameAX(e) };let e:AXUIElement;let role:String;let subrole:String;let text:String;let path:[Int];var children:[Node]=[]
 init(_ e:AXUIElement,_ path:[Int]) { self.e=e;self.path=path;self.role=readAX(e,kAXRoleAttribute) as? String ?? "";self.subrole=readAX(e,kAXSubroleAttribute) as? String ?? "";self.text=self.subrole=="AXSecureTextField" ? "" : Array(Set([readAX(e,kAXTitleAttribute) as? String,readAX(e,kAXValueAttribute) as? String,readAX(e,kAXDescriptionAttribute) as? String].compactMap { $0 }.filter { !$0.isEmpty })).joined(separator:" ") }
 init(role:String,text:String,frame:CGRect,children:[Node]=[]) { self.e=AXUIElementCreateSystemWide();self.role=role;self.subrole="";self.text=text;self.path=[];self.fixtureFrame=frame;self.children=children;for c in children { c.parent=self } }
 func flattened(_ limit:Int=25) -> [Node] { var a:[Node]=[];if !text.isEmpty { a.append(self) };for c in children { a+=c.flattened(limit);if a.count>limit { break } };return a }
}
func sensitive(_ n:Node) -> Bool {
 if n.subrole=="AXSecureTextField" { return true }
 if n.text.range(of:"(?i)(?:输入|enter|type).{0,24}(?:密码|password|验证码|verification code)|sign in (?:to|with) (?:your )?apple|登录(?:你的)?\\s*Apple|需要验证身份|authentication required",options:.regularExpression) != nil { return true }
 if n.role=="AXSheet" || n.role=="AXDialog" || n.subrole=="AXSystemDialog" {
  let text=n.flattened(80).map { $0.text }.joined(separator:" ")
  if text.range(of:"(?i)确认购买|确认更改|确认订阅|confirm (?:purchase|subscription|change)|password|验证码",options:.regularExpression) != nil { return true }
 }
 return false
}
func rowCandidate(_ n:Node) -> (Phrase,String)? {
 if let d=directCandidate(n.text),["AXStaticText","AXButton","AXRow","AXCell","AXGroup","AXLink"].contains(n.role),n.text.count<300 {
  if n.role != "AXGroup" || (n.geometry.map { $0.height<=160 } ?? false) { return (d,"same_accessible_element") }
 }
 guard ["AXRow","AXGroup","AXCell","AXLink"].contains(n.role) else { return nil }
 let leaves=n.flattened(20);guard leaves.count<=20,let row=n.geometry,row.height>0,row.height<=160 else { return nil }
 let labels=leaves.filter { hasPlus($0.text) };guard labels.count==1,let label=labels.first,let lf=label.geometry else { return nil }
 let dated=leaves.filter { phrases($0.text).count==1 };guard dated.count==1,let date=dated.first,let df=date.geometry,abs(lf.midY-df.midY)<=44,let p=phrases(date.text).first else { return nil }
 return (p,"bounded_same_row_group_and_geometry")
}
struct Scan {
 var result:[String:Any]=[:];var phrase:Phrase?=nil;var interrupted=false
}
func scanUI(_ pid:pid_t,_ canceled:()->Bool) -> Scan {
 let begin=Date();let until=begin.addingTimeInterval(3);let app=AXUIElementCreateApplication(pid);AXUIElementSetMessagingTimeout(app,0.12)
 var winRef:CFTypeRef?;let code=AXUIElementCopyAttributeValue(app,kAXWindowsAttribute as CFString,&winRef)
 guard code == .success,let ws=winRef as? [AXUIElement],!ws.isEmpty else { return Scan(result:["pid":pid,"sampled_at":iso(),"AXWindows_status":code.rawValue,"window_count":0]) }
 var nodes=0;var roles:[String:Int]=[:];var allResults:[(Phrase,String,[Int],String,String)]=[];var context=false;var loading=false;var auth=false;var exhaustive=true
 for (wi,w) in ws.enumerated() {
  if canceled() { break };let root=Node(w,[wi]);var todo:[(Node,Int)]=[(root,0)];var inWindow:[Node]=[]
  while let (n,d)=todo.popLast() {
   if canceled() || Date()>until || nodes>=2200 { exhaustive=false;break };nodes+=1;roles[n.role,default:0]+=1;inWindow.append(n)
   if n.text.range(of:"订阅|Subscriptions",options:[.regularExpression,.caseInsensitive]) != nil { context=true }
   if n.subrole=="AXSecureTextField" || n.text.range(of:"(?i)(?:输入|enter|type).{0,24}(?:密码|password|验证码|verification code)|sign in (?:to|with) (?:your )?apple|登录(?:你的)?\\s*Apple|需要验证身份|authentication required",options:.regularExpression) != nil { auth=true;break }
   if n.role=="AXProgressIndicator" || n.text.range(of:"正在载入|正在加载|Loading\\.\\.",options:[.regularExpression,.caseInsensitive]) != nil { loading=true }
   if let cs=readAX(n.e,kAXChildrenAttribute) as? [AXUIElement] {
    if d>=26 || cs.count>160 { if !cs.isEmpty { exhaustive=false } }
    if d<26 { for (i,c) in cs.prefix(160).enumerated() { if canceled() || Date()>until { exhaustive=false;break };n.children.append(Node(c,n.path+[i])) };for c in n.children.reversed() { todo.append((c,d+1)) } }
   }
  }
  for n in inWindow.reversed() { if sensitive(n) { auth=true;break };if let (p,association)=rowCandidate(n) { let rowText=n.flattened(20).map{$0.text}.joined(separator:" ")
    let state=rowText.range(of:"已到期|已于.{0,24}到期|\\bexpired\\b",options:[.regularExpression,.caseInsensitive]) != nil ? "expired" : rowText.range(of:"已取消|取消续订|will not renew|cancelled|canceled",options:[.regularExpression,.caseInsensitive]) != nil ? "canceled" : p.action=="续期" || p.action=="续订" || p.action=="Renews" ? "renewing" : "valid_until"
    let re=try? NSRegularExpression(pattern:"(?i)已到期|已于.{0,24}到期|expired|已取消|取消续订|will not renew|cancelled|canceled")
    let match=re?.firstMatch(in:rowText,range:NSRange(rowText.startIndex...,in:rowText))
    let literal=match.flatMap{Range($0.range,in:rowText)}.map{String(rowText[$0])} ?? p.wording
    allResults.append((p,association,n.path,state,literal)) } }
  if auth { break }
 }
 let values=Dictionary(grouping:allResults,by: { $0.0.wording })
 var j:[String:Any]=["pid":pid,"sampled_at":iso(),"AXWindows_status":code.rawValue,"window_count":ws.count,"nodes_visited":nodes,"role_counts":roles,"subscription_context_seen":context,"loading_indicator_seen":loading,"sensitive_prompt_detected":auth,"scan_complete_within_budget":exhaustive,"raw_page_saved":false,"backend_data_realtime_verified":false,"elapsed_seconds":Date().timeIntervalSince(begin)]
 var selected:Phrase?=nil
 if !auth && context && exhaustive && values.count==1,let first=allResults.sorted(by: { $0.2.count>$1.2.count }).first { selected=first.0;j["candidate"]=first.0.json;j["association"]=first.1;j["AX_role_index_path"]=first.2;j["displayed_state"]=first.3;j["state_wording"]=first.4 }
 return Scan(result:j,phrase:selected,interrupted:auth)
}

import Foundation

// Isolated candidate policy. This never reads or writes production membership data.
struct CleanupLease {
 private(set) var revokedReason:String?
 mutating func revoke(_ reason:String) { if revokedReason==nil { revokedReason=reason } }
 func allows(owned:Bool,frontUnchanged:Bool,permission:Bool,singleWindow:Bool,auth:Bool)->Bool {
  revokedReason==nil && owned && frontUnchanged && permission && singleWindow && !auth
 }
}
struct EvidenceFrame {
 let text:String;let source:String;let sequence:Int;let at:Double
 var complete=true;var loading=false;var authentication=false;var requiresVerification=false
}
struct ConfirmedRecord:Equatable {
 let wording:String;let date:String;let action:String
 init(_ p:Phrase) {wording=p.wording;date=p.date;action=p.action}
}
struct ReadPolicy {
 private(set) var confirmed:ConfirmedRecord?
 private(set) var pending:(EvidenceFrame,Phrase)?
 private(set) var ready:Phrase?
 private(set) var changes=0
 private(set) var status="unverified"
 init(confirmed:ConfirmedRecord?=nil){self.confirmed=confirmed}
 mutating func fail(_ why:String) {pending=nil;ready=nil;status=why}
 mutating func ingest(_ f:EvidenceFrame) {
  guard f.complete,!f.loading,!f.authentication,!f.requiresVerification,
        let p=directCandidate(f.text) else {fail("unverified_frame");return}
  if let old=pending {
   if f.source != old.0.source {fail("source_changed");return}
   if f.sequence<=old.0.sequence {status="duplicate_frame_ignored";return}
   if p != old.1 {fail("state_or_date_conflict");return}
   if f.at-old.0.at>=0.5 {ready=p;status="double_read_waiting_for_cleanup";return}
   status="read_too_soon";return
  }
  pending=(f,p);status="waiting_for_second_read"
 }
 mutating func complete(normalExit:Bool,leaseValid:Bool) {
  guard normalExit,leaseValid,let p=ready else {fail("read_or_cleanup_incomplete");return}
  let record=ConfirmedRecord(p)
  if record != confirmed {confirmed=record;changes+=1}
  pending=nil;ready=nil;status="confirmed"
 }
}

struct DateAssessment {
 let precision:String
 let needsVerification:Bool
 // Never supplies an inferred year or a billing instant.
}
func assessDate(_ phrase:Phrase,today:DateComponents)->DateAssessment {
 let date=phrase.date
 guard let re=try? NSRegularExpression(pattern:"^(?:([0-9]{4})年\\s*)?([0-9]{1,2})月\\s*([0-9]{1,2})日$"),
       let m=re.firstMatch(in:date,range:NSRange(date.startIndex...,in:date)),
       let mr=Range(m.range(at:2),in:date),let dr=Range(m.range(at:3),in:date),
       let month=Int(date[mr]),let day=Int(date[dr]),let ty=today.year,let tm=today.month,let td=today.day else {
  return DateAssessment(precision:"unresolved_displayed_date",needsVerification:true)
 }
 if let yr=Range(m.range(at:1),in:date),let year=Int(date[yr]) {
  return DateAssessment(precision:"explicit_year_month_day",needsVerification:(year,month,day)<(ty,tm,td))
 }
 // Earlier month/day is ambiguous across years. Flag it, without inventing a year.
 return DateAssessment(precision:"displayed_month_day_no_year",needsVerification:(month,day)<(tm,td))
}

import AppKit
import Foundation
import ApplicationServices

let targets = ["com.apple.systempreferences", "com.apple.AppStore"]
func normalized(_ s:String)->String { s.replacingOccurrences(of:"\\s+",with:"",options:.regularExpression).lowercased() }
func semantic(_ n:Node,_ stage:Int)->Bool {
 let s=normalized(n.text)
 if stage==0 { return s.range(of:"apple(?:账户|帐户|account)",options:.regularExpression) != nil && s.count<180 }
 if stage==1 { return ["媒体与购买项目","媒体和购买项目","media&purchases","mediaandpurchases"].contains(s) }
 return ["管理","manage","管理…","manage…"].contains(s)
}
func isSubscription(_ s:String)->Bool { normalized(s).range(of:"订阅|subscriptions",options:.regularExpression) != nil }
func navigationCandidates(_ nodes:[Node],_ stage:Int)->[Node] {
 var found:[Node]=[]
 for leaf in nodes where semantic(leaf,stage) {
  var n:Node?=leaf;var distance=0
  while let c=n,distance<4 {
   if ["AXButton","AXRow","AXLink","AXCheckBox"].contains(c.role) {
    if stage==2 {
     var ancestor:Node?=c;var bounded=false
     for _ in 0..<4 {
      guard let a=ancestor else { break }
      let all=a.flattened(50)
      if let f=a.geometry,f.height<=180,f.height>0,all.count<=50,all.contains(where:{ isSubscription($0.text) }),all.filter({ semantic($0,2) && ["AXButton","AXLink"].contains($0.role) }).count==1 { bounded=true;break }
      ancestor=a.parent
     }
     if !bounded { break }
    }
    if !found.contains(where:{ CFEqual($0.e,c.e) }) { found.append(c) };break
   }
   n=c.parent;distance+=1
  }
 }
 return found
}
func safeCompletion(_ n:Node)->Bool {
 n.role=="AXButton" && ["完成","done","关闭","close"].contains(normalized(n.text))
}
struct Tree { var nodes:[Node]=[];var complete=true;var auth=false;var roots:[Node]=[] }
func tree(_ pid:pid_t,_ canceled:()->Bool)->Tree {
 var t=Tree();let until=Date().addingTimeInterval(2.5)
 let a=AXUIElementCreateApplication(pid);AXUIElementSetMessagingTimeout(a,0.1)
 guard let ws=readAX(a,kAXWindowsAttribute) as? [AXUIElement] else { t.complete=false;return t }
 for (wi,w) in ws.enumerated() {
  let root=Node(w,[wi]);t.roots.append(root);var todo:[(Node,Int)]=[(root,0)]
  while let (n,d)=todo.popLast() {
   if canceled() || Date()>until || t.nodes.count>=2200 { t.complete=false;return t }
   t.nodes.append(n)
   if n.subrole=="AXSecureTextField" || sensitive(n) { t.auth=true;return t }
   if let cs=readAX(n.e,kAXChildrenAttribute) as? [AXUIElement] {
    if d>=26 || cs.count>160 { if !cs.isEmpty { t.complete=false };continue }
    for (i,c) in cs.enumerated() { let child=Node(c,n.path+[i]);child.parent=n;n.children.append(child) }
    for c in n.children.reversed() { todo.append((c,d+1)) }
   }
  }
  // After descendants are available, check modal transaction/authentication context.
  if t.nodes.contains(where:sensitive) { t.auth=true;return t }
 }
 return t
}
func enabled(_ e:AXUIElement)->Bool { (readAX(e,kAXEnabledAttribute) as? Bool)==true }
func press(_ e:AXUIElement)->AXError { AXUIElementPerformAction(e,kAXPressAction as CFString) }
func navigate(_ n:Node)->(String,AXError) {
 guard enabled(n.e) else { return ("disabled",.actionUnsupported) }
 var actions:CFArray?;AXUIElementCopyActionNames(n.e,&actions)
 if (actions as? [String] ?? []).contains(kAXPressAction) { return ("AXPress",press(n.e)) }
 if n.role=="AXRow" {
  var settable=DarwinBoolean(false)
  if AXUIElementIsAttributeSettable(n.e,kAXSelectedAttribute as CFString,&settable) == .success && settable.boolValue {
   return ("set_AXSelected_true",AXUIElementSetAttributeValue(n.e,kAXSelectedAttribute as CFString,kCFBooleanTrue))
  }
 }
 return ("no_safe_navigation_action",.actionUnsupported)
}

final class AppleSubscriptionReader {
 let completion:([String:Any])->Void;let root:URL;let mode:String;let ws=NSWorkspace.shared;let app=NSApplication.shared
 let original:NSRunningApplication?;let baseline:Set<pid_t>;let initialIDs:Set<Int>
 let worker=DispatchQueue(label:"cc.nuoye.renewal.readonly.serial")
 let lock=NSLock();var cancel=false
 var timer:Timer?;var token:NSObjectProtocol?;var started:Date?;var deadline=Date.distantFuture;var stopping=false;var finished=false;var busy=false;var callback=false;var cleaned=false
 var lease=CleanupLease();var extraTokens:[NSObjectProtocol]=[]
 var reason="";var launched:NSRunningApplication?;var stage=0;var actionCount=0;var urlCount=0
 var actions:[[String:Any]]=[];var cleanActions:[[String:Any]]=[];var observations:[[String:Any]]=[];var activations:[[String:Any]]=[];var records:[Int:[String:Any]]=[:];var identities:[pid_t:[String:Any]]=[:]
 var stateWording="";var priorDisplayedState:String?;var displayedState="unknown";var firstValueAt:Date?;var stableAt:Date?;var value:Phrase?;var prior:Phrase?;var priorPID:pid_t?;var priorAt=Date.distantPast;var stableCount=0;var lastScan=Date.distantPast;var cleanupAt:Date?;var lastCleanup=Date.distantPast;var quitAt:[pid_t:Date]=[:]
 init(_ r:URL,_ m:String,completion:@escaping([String:Any])->Void) { self.completion=completion;root=r;mode=m;original=ws.frontmostApplication;baseline=Set(ws.runningApplications.map{$0.processIdentifier});initialIDs=Set((windows() ?? []).compactMap{$0[kCGWindowNumber as String] as? Int}) }
 func canceled()->Bool { lock.lock();defer{lock.unlock()};return cancel }
 func setCancel() { lock.lock();cancel=true;lock.unlock() }
 func disturbed()->Bool { ws.frontmostApplication?.processIdentifier != original?.processIdentifier || FileManager.default.fileExists(atPath:root.appendingPathComponent("user-stop.signal").path) }
 func relinquish(_ why:String) {lock.lock();lease.revoke(why);lock.unlock()}
 func leaseReason()->String? {lock.lock();defer{lock.unlock()};return lease.revokedReason}
 func sessionAvailable()->Bool {
  guard let info=CGSessionCopyCurrentDictionary() as? [String:Any],
        info[kCGSessionOnConsoleKey as String] as? Bool==true,
        info[kCGSessionLoginDoneKey as String] as? Bool==true,
        CGDisplayIsAsleep(CGMainDisplayID())==0 else{return false}
  let front=ws.frontmostApplication?.bundleIdentifier ?? ""
  if front.isEmpty || front=="com.apple.loginwindow" || front.lowercased().contains("screensaver") {return false}
  guard let currentWindows=windows() else{return false}
  if currentWindows.contains(where:{($0[kCGWindowOwnerName as String] as? String)=="loginwindow" && ($0[kCGWindowIsOnscreen as String] as? Bool)==true}) {return false}
  return true
 }
 func owned()->[NSRunningApplication] {
  guard let a=launched,!a.isTerminated,!baseline.contains(a.processIdentifier),
        a.bundleIdentifier==targets[1],a.executableURL?.path=="/System/Applications/App Store.app/Contents/MacOS/App Store" else{return []}
  return [a]
 }
 func cleanupPermitted(_ a:NSRunningApplication,_ single:Bool=true,_ auth:Bool=false)->Bool {
  lock.lock();let l=lease;lock.unlock()
  guard l.allows(owned:owned().contains(where:{$0.processIdentifier==a.processIdentifier}),frontUnchanged:!disturbed() && sessionAvailable(),permission:AXIsProcessTrusted(),singleWindow:single,auth:auth) else{return false}
  let e=AXUIElementCreateApplication(a.processIdentifier);AXUIElementSetMessagingTimeout(e,0.08)
  guard let current=readAX(e,kAXWindowsAttribute) as? [AXUIElement],current.count<=1 else{relinquish("last_moment_window_ownership_uncertain");return false}
  return !disturbed() && leaseReason()==nil
 }
 func begin() {
  if mode=="permission-check" { finish("permission_check_only");return }
  if mode=="selftest" { selftest();return }
  let fixtures=["fixture-takeover":"user_takeover","fixture-auth":"auth_prompt","fixture-concurrent":"concurrent_entry","fixture-permission":"permission_lost"]
  if let why=fixtures[mode] {relinquish(why);stopping=true;reason="offline_control_fixture";cleanupAt=Date();cleanup();return}
  guard mode=="B" else { finish("invalid_mode");return }
  guard AXIsProcessTrusted() else { finish("skipped_permission_missing_no_prompt");return }
  guard sessionAvailable() else {finish("skipped_session_or_display_unavailable");return}
  guard targets.allSatisfy({NSRunningApplication.runningApplications(withBundleIdentifier:$0).isEmpty}) else { finish("blocked_user_target_app_open");return }
  guard windows() != nil else { finish("window_metadata_unavailable");return }
  guard !FileManager.default.fileExists(atPath:root.appendingPathComponent("attempt-ledger.json").path) else { finish("already_attempted");return }
  let bundle=mode=="A" ? targets[0] : targets[1]
  guard let application=ws.urlForApplication(withBundleIdentifier:bundle) else { finish("target_unavailable");return }
  save(["at":iso(),"front_bundle":original?.bundleIdentifier ?? "none","target_apps_running":false,"baseline_processes":baseline.sorted()],root.appendingPathComponent("baseline.json"))
  token=ws.notificationCenter.addObserver(forName:NSWorkspace.didActivateApplicationNotification,object:nil,queue:.main) { [weak self] n in
   guard let self=self,let a=n.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication else{return}
   self.activations.append(["at":iso(),"pid":a.processIdentifier,"bundle":a.bundleIdentifier ?? "unknown"])
   if a.processIdentifier != self.original?.processIdentifier { self.relinquish("foreground_changed_or_user_takeover");self.stop("foreground_changed") }
  }
  for name in [NSWorkspace.sessionDidResignActiveNotification,NSWorkspace.screensDidSleepNotification] {
   extraTokens.append(ws.notificationCenter.addObserver(forName:name,object:nil,queue:.main){[weak self] _ in self?.relinquish("session_or_display_unavailable");self?.stop("session_or_display_unavailable")})
  }
  timer=Timer.scheduledTimer(withTimeInterval:0.1,repeats:true) { [weak self] _ in self?.tick() }
  let cfg=NSWorkspace.OpenConfiguration();cfg.activates=false;cfg.hides=false;cfg.promptsUserIfNeeded=false;cfg.addsToRecentItems=false;cfg.createsNewApplicationInstance=false;cfg.allowsRunningApplicationSubstitution=false
  guard targets.allSatisfy({NSRunningApplication.runningApplications(withBundleIdentifier:$0).isEmpty}) else{finish("skipped_concurrent_target_before_request");return}
  started=Date();deadline=started!.addingTimeInterval(45)
  save(["mode":mode,"started_at":iso(started!),"maximum_read_seconds":45,"attempt_count":1],root.appendingPathComponent("attempt-ledger.json"))
  let completion:(NSRunningApplication?,Error?)->Void = { [weak self] a,e in DispatchQueue.main.async {
   guard let self=self else{return};self.callback=true
   if e != nil { self.stop("launch_error");return }
   guard let a=a,a.bundleIdentifier==bundle,!self.baseline.contains(a.processIdentifier) else { self.stop("unexpected_or_preexisting_target");return }
   self.launched=a
  } }
  if mode=="A" { ws.openApplication(at:application,configuration:cfg,completionHandler:completion) }
  else { urlCount=1;ws.open([URL(string:"https://apps.apple.com/account/subscriptions")!],withApplicationAt:application,configuration:cfg,completionHandler:completion) }
 }
 func recordWindows() {
  for (order,w) in (windows() ?? []).enumerated() {
   guard let id=w[kCGWindowNumber as String] as? Int,!initialIDs.contains(id) else {continue}
   var j=records[id] ?? ["id":id,"owner_pid":w[kCGWindowOwnerPID as String] ?? 0,"first_at":iso()]
   j["last_at"]=iso();j["onscreen"]=w[kCGWindowIsOnscreen as String] ?? false;j["layer"]=w[kCGWindowLayer as String] ?? -1;j["alpha"]=w[kCGWindowAlpha as String] ?? -1;j["bounds"]=w[kCGWindowBounds as String] ?? [:];j["front_to_back_index"]=order;records[id]=j
  }
  for a in owned() where identities[a.processIdentifier]==nil { identities[a.processIdentifier]=["pid":a.processIdentifier,"bundle":a.bundleIdentifier ?? "unknown","executable":a.executableURL?.path ?? "unknown","first_seen_at":iso()] }
 }
 func tick() {
  if finished{return};recordWindows()
  if stopping { cleanup();return }
  if disturbed() { relinquish("foreground_changed_or_user_stop");stop("foreground_changed_or_user_stop");return }
  if !sessionAvailable() {relinquish("session_or_display_unavailable");stop("session_or_display_unavailable");return}
  if !AXIsProcessTrusted() {relinquish("permission_lost");stop("permission_lost_no_prompt");return}
  if Date()>=deadline { stop("timeout_45_seconds");return }
  guard callback,launched != nil,!busy,Date().timeIntervalSince(lastScan)>=0.65 else{return}
  busy=true;lastScan=Date();let apps=owned();let navStage=stage
  worker.async {
   var results:[Scan]=[];var navigation:[String:Any]?=nil
   for a in apps {
    if self.canceled(){break}
    let s=scanUI(a.processIdentifier,self.canceled);results.append(s)
    if s.interrupted { break }
   }
   if self.mode=="A",navStage<3,!results.contains(where:{$0.interrupted}),let settings=apps.first(where:{$0.bundleIdentifier==targets[0]}),!self.canceled() {
    let t=tree(settings.processIdentifier,self.canceled)
    if t.auth { results.append(Scan(result:["pid":settings.processIdentifier,"sensitive_prompt_detected":true],interrupted:true)) }
    else if t.complete {
     let candidates=navigationCandidates(t.nodes,navStage)
     if candidates.count==1,let n=candidates.first,!self.canceled(),!self.disturbed() {
      let (action,code)=navigate(n)
      navigation=["at":iso(),"pid":settings.processIdentifier,"bundle":targets[0],"step":["apple_account","media_purchases","subscriptions_manage"][navStage],"action":action,"AX_status":code.rawValue,"success":code == .success]
     }
    }
   }
   DispatchQueue.main.async { self.scanned(results,navigation) }
  }
 }
 func scanned(_ data:[Scan],_ nav:[String:Any]?) {
  busy=false
  if let nav=nav { actions.append(nav);actionCount+=1;if nav["success"] as? Bool==true {stage+=1} else {stop("read_only_navigation_action_failed")} }
  if stopping || finished{return}
  for s in data { if observations.count<150 { observations.append(s.result) };if s.interrupted { relinquish("authentication_or_transaction_prompt");stop("authentication_or_transaction_prompt");return } }
  let eligible=data.filter { $0.phrase != nil && $0.result["loading_indicator_seen"] as? Bool != true && $0.result["scan_complete_within_budget"] as? Bool == true }
  let distinct=Set(eligible.compactMap{$0.phrase?.wording})
  if distinct.count>1 {relinquish("conflicting_ui_values");stop("conflicting_ui_values");return}
  if eligible.contains(where:{($0.result["window_count"] as? Int ?? 0)>1}) {relinquish("unowned_or_concurrent_window");stop("unowned_or_concurrent_window");return}
  guard let s=eligible.first,let p=s.phrase,let pid=s.result["pid"] as? pid_t else { stableCount=0;return }
  if firstValueAt==nil {firstValueAt=Date()}
  let currentDisplayedState=s.result["displayed_state"] as? String ?? "unknown"
  if p==prior,pid==priorPID,currentDisplayedState==priorDisplayedState,Date().timeIntervalSince(priorAt)>=0.5 {stableCount+=1} else {stableCount=1}
  prior=p;priorPID=pid;priorDisplayedState=currentDisplayedState;priorAt=Date()
  if stableCount>=2 {value=p;stateWording=s.result["state_wording"] as? String ?? p.wording;displayedState=s.result["displayed_state"] as? String ?? "unknown";stableAt=Date();stop("confirmed_ui_row_read")}
 }
 func stop(_ why:String) { if stopping || finished{return};reason=why;stopping=true;setCancel();cleanupAt=Date();cleanup() }
 func cleanup() {
  if finished || busy{return}
  if disturbed() {relinquish("foreground_changed_or_user_stop")}
  if !sessionAvailable() {relinquish("session_or_display_unavailable")}
  if !AXIsProcessTrusted() {relinquish("permission_lost")}
  if let blocked=leaseReason() {finish(reason+"_preserved_no_cleanup_"+blocked);return}
  if !callback,Date().timeIntervalSince(cleanupAt ?? Date())<3{return}
  let apps=owned()
  if apps.isEmpty {cleaned=true;finish(reason);return}
  if Date().timeIntervalSince(cleanupAt ?? Date())>=12 {finish(reason+"_cleanup_incomplete");return}
  guard Date().timeIntervalSince(lastCleanup)>=0.8 else{return};lastCleanup=Date();busy=true
  let a=apps[0]
  worker.async {
   let t=tree(a.processIdentifier,{Date().timeIntervalSince(self.cleanupAt ?? Date())>11 || self.leaseReason() != nil})
   var action:[String:Any]?=nil
   if t.auth {self.relinquish("auth_or_transaction_during_cleanup")}
   if t.roots.count>1 {self.relinquish("concurrent_or_ambiguous_windows")}
   if !t.complete {self.relinquish("cleanup_tree_unverified")}
   if t.complete && self.cleanupPermitted(a,t.roots.count<=1,t.auth) {
    let sheets=t.nodes.filter{["AXSheet","AXDialog"].contains($0.role)}
    let completions=t.nodes.filter{n in
     guard safeCompletion(n),enabled(n.e) else{return false}
     var p=n.parent
     while let ancestor=p {if sheets.contains(where:{$0===ancestor}){return true};p=ancestor.parent}
     return false
    }
    if completions.count>1 {self.relinquish("ambiguous_completion_controls")}
    else if let n=completions.first,self.cleanupPermitted(a) {
     action=["action":"safe_sheet_completion_AXPress","pid":a.processIdentifier,"AX_status":press(n.e).rawValue,"at":iso()]
    } else if sheets.isEmpty,let w=t.roots.first,let close=readAX(w.e,kAXCloseButtonAttribute),CFGetTypeID(close)==AXUIElementGetTypeID() {
     let e=unsafeBitCast(close,to:AXUIElement.self)
     if enabled(e),self.cleanupPermitted(a) {action=["action":"standard_window_close_AXPress","pid":a.processIdentifier,"AX_status":press(e).rawValue,"at":iso()]}
    }
   }
   DispatchQueue.main.async {
    self.busy=false;if let action=action {self.cleanActions.append(action)}
    if let blocked=self.leaseReason() {self.finish(self.reason+"_preserved_no_further_cleanup_"+blocked);return}
    // Recheck current ownership, session, trust and foreground immediately before quit.
    guard self.cleanupPermitted(a) else{self.relinquish("last_moment_ownership_uncertain");self.finish(self.reason+"_preserved_no_quit");return}
    if self.quitAt[a.processIdentifier]==nil || Date().timeIntervalSince(self.quitAt[a.processIdentifier]!)>2 {
     self.quitAt[a.processIdentifier]=Date();let accepted=a.terminate()
     self.cleanActions.append(["action":"NSRunningApplication_terminate","pid":a.processIdentifier,"accepted":accepted,"at":iso(),"force":false])
    }
   }
  }
 }
 func finish(_ status:String) {
  if finished{return};finished=true;setCancel();timer?.invalidate();if let token=token {ws.notificationCenter.removeObserver(token)};for t in extraTokens {ws.notificationCenter.removeObserver(t)}
  var j:[String:Any]=["outcome":status,"at":iso(),"mode":mode,"probe_AX_trusted":AXIsProcessTrusted(),"probe_bundle":Bundle.main.bundleIdentifier ?? "none","probe_executable":Bundle.main.executableURL?.path ?? "none","start_at":started.map{iso($0)} ?? NSNull(),"navigation_actions":actions,"navigation_count":actionCount,"official_url_count":urlCount,"owned_processes":Array(identities.values),"cleanup_actions":cleanActions,"owned_apps_still_running":owned().map{["pid":$0.processIdentifier,"bundle":$0.bundleIdentifier ?? "none"]},"normal_cleanup_complete":started==nil ? true : cleaned,"force_termination_used":false,"total_seconds":started.map{Date().timeIntervalSince($0)} ?? 0,"first_valid_row_seconds":firstValueAt.flatMap{v in started.map{v.timeIntervalSince($0)}} ?? NSNull(),"stable_row_seconds":stableAt.flatMap{v in started.map{v.timeIntervalSince($0)}} ?? NSNull(),"cleanup_seconds":cleanupAt.map{Date().timeIntervalSince($0)} ?? 0,"initial_front_bundle":original?.bundleIdentifier ?? "none","final_front_bundle":ws.frontmostApplication?.bundleIdentifier ?? "none","activation_events":activations,"window_records":Array(records.values),"AX_scans":observations,"sensitive_raw_page_saved":false,"backend_realtime_verified":false,"user_perception":"pending human observation","maximum_read_seconds":45,"maximum_normal_cleanup_seconds":12,"screen_or_full_disk_permission_requested":false]
  j["cleanup_lease_revoked_reason"]=leaseReason() ?? NSNull();j["production_membership_write"]=false;j["displayed_state"]=displayedState;j["state_wording"]=stateWording
  if let value=value {j["extracted"]=value.json;j["source_pid"]=priorPID ?? 0}
  save(j,root.appendingPathComponent("result-"+mode+".json"));completion(j)
 }
 func selftest() {
  var cases:[[String:Any]]=[]
  func check(_ name:String,_ passed:Bool){cases.append(["case":name,"passed":passed])}
  let f=CGRect(x:0,y:0,width:440,height:82)
  check("AXLink direct product row accepted",rowCandidate(Node(role:"AXLink",text:"ChatGPT Plus 4月12日续期",frame:f)) != nil)
  let label=Node(role:"AXStaticText",text:"ChatGPT Plus",frame:CGRect(x:0,y:0,width:100,height:22))
  let date=Node(role:"AXStaticText",text:"4月12日续期",frame:CGRect(x:0,y:28,width:100,height:22))
  check("AXLink child labels associated within bounded row",rowCandidate(Node(role:"AXLink",text:"",frame:f,children:[label,date])) != nil)
  check("large page ancestor rejected",rowCandidate(Node(role:"AXGroup",text:"",frame:CGRect(x:0,y:0,width:440,height:530),children:[label,date]))==nil)
  check("other product rejected",directCandidate("Other Product 4月12日续期")==nil)
  check("invalid calendar date rejected",directCandidate("ChatGPT Plus 2月30日续期")==nil)
  check("two different dates rejected",directCandidate("ChatGPT Plus 4月12日续期 5月12日续期")==nil)
  check("cancelled renewal rejected",directCandidate("ChatGPT Plus 已取消 4月12日续期")==nil)
  check("cancelled expiry accepted",directCandidate("ChatGPT Plus 已取消 4月12日到期") != nil)
  check("English renewal accepted",directCandidate("ChatGPT Plus Renews on June 10, 2031") != nil)
  check("no date inferred from purchase date",directCandidate("ChatGPT Plus 购买日期2031年4月12日")==nil)
  check("done completion accepted",safeCompletion(Node(role:"AXButton",text:"完成",frame:f)))
  for s in ["取消订阅","续订","购买","确认","Cancel Subscription","Renew","Buy"] {check("reject cleanup action "+s,!safeCompletion(Node(role:"AXButton",text:s,frame:f)))}
  let manage=Node(role:"AXButton",text:"管理",frame:f)
  let row=Node(role:"AXGroup",text:"",frame:f,children:[Node(role:"AXStaticText",text:"订阅",frame:f),manage])
  check("subscriptions manage associated",navigationCandidates([row,manage],2).count==1)
  let other=Node(role:"AXButton",text:"管理",frame:f)
  let wrong=Node(role:"AXGroup",text:"",frame:f,children:[Node(role:"AXStaticText",text:"付款方式",frame:f),other])
  check("unrelated manage rejected",navigationCandidates([wrong,other],2).isEmpty)
  check("apple account whitespace localized",semantic(Node(role:"AXRow",text:"Apple 账户",frame:f),0))
  check("media and purchases exact localized",semantic(Node(role:"AXButton",text:"媒体与购买项目",frame:f),1))
  observations=cases;finish(cases.allSatisfy{$0["passed"] as? Bool==true} ? "selftest_passed":"selftest_failed")
 }
}
