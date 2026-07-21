"use client"

import { useTheme } from "next-themes"
import { Toaster as Sonner, type ToasterProps } from "sonner"
import { CircleCheckIcon, InfoIcon, TriangleAlertIcon, OctagonXIcon, Loader2Icon } from "lucide-react"

const Toaster = ({ ...props }: ToasterProps) => {
  const { theme = "system" } = useTheme()

  return (
    <Sonner
      theme={theme as ToasterProps["theme"]}
      className="toaster group"
      icons={{
        success: (
          <CircleCheckIcon className="size-4" />
        ),
        info: (
          <InfoIcon className="size-4" />
        ),
        warning: (
          <TriangleAlertIcon className="size-4" />
        ),
        error: (
          <OctagonXIcon className="size-4" />
        ),
        loading: (
          <Loader2Icon className="size-4 animate-spin" />
        ),
      }}
      style={
        {
          "--width": "min(340px, calc(100vw - 24px))",
          "--normal-bg": "rgba(247, 246, 243, 0.78)",
          "--normal-text": "var(--popover-foreground)",
          "--normal-border": "rgba(255, 255, 255, 0.58)",
          "--border-radius": "18px",
        } as React.CSSProperties
      }
      toastOptions={{
        style: {
          backdropFilter: "blur(24px) saturate(1.22)",
          WebkitBackdropFilter: "blur(24px) saturate(1.22)",
        },
        classNames: {
          toast:
            "cn-toast pointer-events-none !min-h-0 !gap-2.5 !px-3.5 !py-3 !shadow-[0_16px_42px_rgba(30,32,44,0.16),inset_0_1px_0_rgba(255,255,255,0.72)]",
          content: "min-w-0 gap-0.5",
          title: "text-[13px] font-semibold leading-5",
          description: "text-[11px] leading-4 !text-muted-foreground",
          icon: "!mr-0 text-foreground/75",
          actionButton:
            "pointer-events-auto !h-7 !rounded-full !bg-foreground !px-3 !text-[11px] !font-semibold !text-background hover:!bg-foreground/85",
          cancelButton:
            "pointer-events-auto !h-7 !rounded-full !bg-foreground/[0.07] !px-3 !text-[11px] !font-semibold !text-foreground hover:!bg-foreground/[0.11]",
        },
      }}
      {...props}
    />
  )
}

export { Toaster }
