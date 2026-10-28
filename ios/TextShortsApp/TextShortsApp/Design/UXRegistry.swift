import SwiftUI

enum UXID: String, CaseIterable {
    case ux001 = "UX-001", ux002 = "UX-002", ux004 = "UX-004", ux006 = "UX-006", ux007 = "UX-007"
    case ux008 = "UX-008", ux009 = "UX-009", ux010 = "UX-010", ux011 = "UX-011", ux012 = "UX-012", ux013 = "UX-013"
    case ux014 = "UX-014", ux015 = "UX-015", ux016 = "UX-016", ux017 = "UX-017", ux018 = "UX-018", ux019 = "UX-019"
    case ux020 = "UX-020", ux021 = "UX-021", ux022 = "UX-022", ux023 = "UX-023", ux024 = "UX-024", ux025 = "UX-025", ux026 = "UX-026"
    case ux027 = "UX-027", ux028 = "UX-028", ux029 = "UX-029", ux030 = "UX-030", ux031 = "UX-031", ux032 = "UX-032", ux033 = "UX-033", ux034 = "UX-034", ux035 = "UX-035", ux036 = "UX-036"
    case ux037 = "UX-037", ux038 = "UX-038", ux039 = "UX-039", ux040 = "UX-040", ux041 = "UX-041", ux042 = "UX-042", ux043 = "UX-043", ux044 = "UX-044", ux045 = "UX-045", ux046 = "UX-046"
    case ux047 = "UX-047", ux048 = "UX-048", ux049 = "UX-049", ux050 = "UX-050", ux051 = "UX-051", ux052 = "UX-052", ux053 = "UX-053", ux054 = "UX-054", ux055 = "UX-055", ux056 = "UX-056", ux057 = "UX-057"
    case ux058 = "UX-058", ux059 = "UX-059", ux060 = "UX-060", ux061 = "UX-061", ux062 = "UX-062", ux063 = "UX-063", ux064 = "UX-064", ux065 = "UX-065", ux066 = "UX-066", ux067 = "UX-067"
    case ux068 = "UX-068", ux069 = "UX-069", ux070 = "UX-070", ux071 = "UX-071"
    case ux072 = "UX-072", ux073 = "UX-073", ux074 = "UX-074", ux075 = "UX-075", ux076 = "UX-076"
    case ux077 = "UX-077", ux078 = "UX-078", ux079 = "UX-079", ux080 = "UX-080", ux081 = "UX-081", ux082 = "UX-082"
    case ux083 = "UX-083", ux084 = "UX-084", ux085 = "UX-085", ux086 = "UX-086", ux087 = "UX-087", ux088 = "UX-088", ux089 = "UX-089"
    case ux090 = "UX-090"
    case ux091 = "UX-091", ux092 = "UX-092", ux093 = "UX-093", ux094 = "UX-094"
    case ux095 = "UX-095", ux096 = "UX-096", ux097 = "UX-097", ux098 = "UX-098", ux099 = "UX-099", ux100 = "UX-100"
    case ux101 = "UX-101", ux102 = "UX-102", ux103 = "UX-103"
    case ux104 = "UX-104", ux105 = "UX-105", ux106 = "UX-106", ux107 = "UX-107"
    case ux108 = "UX-108", ux109 = "UX-109", ux110 = "UX-110", ux111 = "UX-111", ux112 = "UX-112"
    case ux113 = "UX-113", ux114 = "UX-114", ux115 = "UX-115"
    case ux119 = "UX-119", ux120 = "UX-120"
    case ux121 = "UX-121", ux122 = "UX-122", ux123 = "UX-123", ux124 = "UX-124"
    case ux125 = "UX-125", ux126 = "UX-126"
    case ux127 = "UX-127", ux128 = "UX-128", ux129 = "UX-129", ux130 = "UX-130", ux131 = "UX-131"
    case ux132 = "UX-132"
    case ux133 = "UX-133", ux134 = "UX-134", ux135 = "UX-135", ux136 = "UX-136"
    case ux137 = "UX-137", ux138 = "UX-138", ux139 = "UX-139"
}

enum ScreenID: String, CaseIterable {
    case scr01 = "SCR-01", scr02 = "SCR-02", scr03 = "SCR-03", scr04 = "SCR-04", scr05 = "SCR-05"
    case scr06 = "SCR-06", scr07 = "SCR-07", scr08 = "SCR-08", scr09 = "SCR-09"
    case scr10 = "SCR-10"
    case scr11 = "SCR-11"
}

private struct UXTagModifier: ViewModifier {
    let id: String
    let show: Bool

    func body(content: Content) -> some View {
        content
            .accessibilityIdentifier(id)
            .overlay {
                if show {
                    RoundedRectangle(cornerRadius: 3)
                        .stroke(Color(red: 0.05, green: 0.48, blue: 0.46), style: StrokeStyle(lineWidth: 0.75, dash: [3, 2]))
                        .allowsHitTesting(false)
                }
            }
            .overlay(alignment: .topTrailing) {
                if show {
                    Text(id)
                        .font(.system(size: 8, weight: .bold, design: .monospaced))
                        .foregroundColor(Color(red: 0.96, green: 0.95, blue: 0.91))
                        .padding(.horizontal, 4)
                        .padding(.vertical, 2)
                        .background(Color(red: 0.03, green: 0.38, blue: 0.36))
                        .clipShape(RoundedRectangle(cornerRadius: 4))
                        .offset(x: 4, y: -7)
                        .allowsHitTesting(false)
                        .accessibilityHidden(true)
                }
            }
    }
}

private struct ScreenTagModifier: ViewModifier {
    let id: String
    let show: Bool

    func body(content: Content) -> some View {
        content
            .overlay(alignment: .topLeading) {
                Text(id)
                    .font(.system(size: 1))
                    .frame(width: 1, height: 1)
                    .opacity(0.01)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(id)
                    .accessibilityIdentifier(id)
                    .allowsHitTesting(false)
            }
            .overlay {
                if show {
                    Rectangle()
                        .stroke(Color(red: 0.05, green: 0.48, blue: 0.46), style: StrokeStyle(lineWidth: 0.75, dash: [3, 2]))
                        .allowsHitTesting(false)
                        .accessibilityHidden(true)
                }
            }
            .overlay(alignment: .topLeading) {
                if show {
                    Text(id)
                        .font(.system(size: 8, weight: .bold, design: .monospaced))
                        .foregroundColor(Color(red: 0.96, green: 0.95, blue: 0.91))
                        .padding(.horizontal, 4)
                        .padding(.vertical, 2)
                        .background(Color(red: 0.03, green: 0.38, blue: 0.36))
                        .offset(x: 4, y: 4)
                        .allowsHitTesting(false)
                        .accessibilityHidden(true)
                }
            }
    }
}

extension View {
    func uxTagged(_ id: UXID, show: Bool) -> some View {
        modifier(UXTagModifier(id: id.rawValue, show: show))
    }

    func screenTagged(_ id: ScreenID, show: Bool) -> some View {
        modifier(ScreenTagModifier(id: id.rawValue, show: show))
    }

    func prototypeTagged(_ id: String) -> some View {
        accessibilityIdentifier(id)
    }
}
