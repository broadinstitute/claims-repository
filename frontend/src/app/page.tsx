import { redirect } from "next/navigation";

// The public claims repository frontend will live at the root (browse claims,
// propositions and their evidence; run query templates). Until it exists, the
// root opens the admin console.
export default function Home() {
  redirect("/admin");
}
